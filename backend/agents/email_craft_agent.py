"""EmailCraftAgent — concurrent N-step email sequence generation per lead, multi-language.

Uses a ReAct loop (Think → Draft → Validate → Revise) with max 3 iterations.
The validate_emails tool checks language correctness, formality, salutation format,
and cultural norms per locale before the agent finalises the output.

Default sequence length is 1 (Settings.email_sequence_steps); the
``_EMAIL_STEP_SPECS`` table keeps up to 3 entries so operators can
opt back into a multi-step cadence by bumping the setting.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from config.settings import get_settings
from emailing.body_format import format_email_sequence_bodies, format_plaintext_email_body
from emailing.policy import (
    choose_email_target,
    expand_email_targets,
    is_role_based_email,
)
from emailing.template_pipeline import compose_template_plan, extract_template_profile
from graph.state import HuntState
from tools.llm_client import LLMTool
from tools.llm_output import (
    EMAIL_SEQUENCE_DEFAULTS,
    EMAIL_SEQUENCE_REQUIRED,
    parse_json,
    validate_dict,
)
from tools.react_runner import ToolDef, react_loop

logger = logging.getLogger(__name__)

_DEFAULT_TEMPLATE_MAX_SEND_COUNT = 100


def _recipient_type_hint(target: dict[str, Any]) -> str:
    """Render a one-line human note for the prompt so the LLM knows
    whether it's writing to a named person or a shared inbox. The
    CTA + tone guidance differ: role-based mailboxes usually bounce
    around inside a company before landing, so the message should be
    crisp and self-explanatory.
    """
    if not target:
        return "No recipient resolved."
    email = str(target.get("target_email") or "").strip()
    if not email:
        return "No recipient resolved."
    name = str(target.get("target_name") or "").strip()
    title = str(target.get("target_title") or "").strip()
    role_based = bool(target.get("is_role_based"))
    target_type = str(target.get("target_type") or "").strip()
    lines: list[str] = []
    lines.append(f"Recipient: {name or '—'} <{email}>")
    if title:
        lines.append(f"Title: {title}")
    lines.append(f"Source: {target_type or 'unknown'}")
    if role_based:
        lines.append(
            "Note: this is a role-based / shared inbox. The message will be "
            "triaged by an assistant before it reaches a person, so keep the "
            "subject line concrete (include our product name + their company) "
            "and put the actionable ask + signature in the first two body "
            "paragraphs. Do NOT expect a direct personal reply."
        )
    else:
        lines.append(
            "Note: this is a named decision maker. The subject can be more "
            "tailored and the body can build rapport before the ask."
        )
    return "\n".join(lines)


def _format_hunter_contacts(contacts: list[dict[str, Any]]) -> str:
    """Render Hunter.io discovered contacts as a prompt block. Empty
    list returns a clear "not available" marker so the LLM doesn't
    silently fall back to guessing.
    """
    if not contacts:
        return (
            "None available for this lead (Hunter.io wasn't called, was "
            "disabled, returned nothing, or the lead already had named "
            "decision makers). The recipient is therefore the chosen "
            "target above — write directly to them."
        )
    lines: list[str] = []
    for c in contacts[:5]:
        email = str(c.get("email") or "")
        first = str(c.get("first_name") or "").strip()
        last = str(c.get("last_name") or "").strip()
        position = str(c.get("position") or "").strip()
        seniority = str(c.get("seniority") or "").strip()
        department = str(c.get("department") or "").strip()
        confidence = c.get("confidence", 0)
        role = " ".join(part for part in (first, last) if part).strip() or "—"
        meta_bits = [bit for bit in (position, seniority, department) if bit]
        meta = " / ".join(meta_bits) if meta_bits else "no role metadata"
        lines.append(
            f"- {role} <{email}> — {meta} (Hunter confidence {confidence}%)"
        )
    return "\n".join(lines)


def _domain_from_url(url: str) -> str:
    """Extract a bare hostname from a URL. Returns ``""`` for
    anything that doesn't look like a valid domain.
    """
    if not url:
        return ""
    text = str(url).strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        host = urlparse(text).hostname or ""
    except ValueError:
        return ""
    host = host.lower().strip()
    # Drop a leading "www." so the Hunter lookup matches whatever
    # they actually expose via MX.
    if host.startswith("www."):
        host = host[4:]
    if "." not in host:
        return ""
    return host


async def _enrich_lead_with_hunter(
    lead: dict[str, Any],
    *,
    max_results: int = 5,
) -> list[dict[str, Any]]:
    """If the lead has no named decision makers, ask Hunter.io to
    discover contacts at the lead's domain. Returns a list of
    ``{email, first_name, last_name, position, seniority, department,
    confidence}`` dicts. Empty list on any failure (missing key,
    domain, quota, network, …) — callers fall back to role-based
    targets and the LLM prompt.
    """
    # Per product decision: only call Hunter when the lead has no
    # decision_makers at all (otherwise we burn quota on leads that
    # already have named contacts).
    if lead.get("decision_makers"):
        return []
    website = str(lead.get("website") or "").strip()
    if not website:
        return []
    domain = _domain_from_url(website)
    if not domain:
        return []
    try:
        from tools.hunter_client import HunterClient, HunterError
    except ImportError:
        return []
    try:
        client = HunterClient.from_settings()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EmailCraft] Hunter client init skipped: %s", exc)
        return []
    try:
        response = await client.domain_search(domain, limit=max_results)
    except Exception as exc:  # noqa: BLE001 — HunterError + httpx + quota
        logger.info(
            "[EmailCraft] Hunter domain_search(%s) skipped: %s",
            domain, exc.__class__.__name__,
        )
        return []
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass
    # Filter to deliverable + personal contacts (skip generic role
    # addresses that Hunter may also surface).
    out: list[dict[str, Any]] = []
    for e in response.emails:
        if not e.first_name and not e.last_name:
            # Pure role-based / generic — Hunter is just echoing
            # public catch-alls. Skip so we don't get duplicates
            # of the role-based inbox we already have.
            continue
        out.append({
            "email": e.email,
            "first_name": e.first_name,
            "last_name": e.last_name,
            "position": e.position,
            "seniority": e.seniority,
            "department": e.department,
            "confidence": e.confidence,
            "source": "hunter.io",
            "is_role_based": is_role_based_email(e.email),
        })
    return out


# ── Locale rules: validation criteria per language ────────────────────────────
_LOCALE_RULES: dict[str, dict[str, Any]] = {
    "de": {
        "language": "German", "formality": "formal", "script": "latin",
        "salutation": "Sehr geehrte(r) Damen und Herren / Sehr geehrte(r) [Name]",
        "closing": "Mit freundlichen Grüßen",
        "checks": [
            "All text must be in German (no English sentences)",
            "Use formal 'Sie' (not 'du') throughout",
            "Subject line must be in German",
            "Use proper German umlauts: ä, ö, ü, ß",
        ],
    },
    "fr": {
        "language": "French", "formality": "formal", "script": "latin",
        "salutation": "Madame, Monsieur / Madame [Nom] / Monsieur [Nom]",
        "closing": "Veuillez agréer, Madame/Monsieur, l'expression de mes salutations distinguées",
        "checks": [
            "All text must be in French (no English sentences)",
            "Use formal 'vous' (not 'tu')",
            "Subject line must be in French",
            "Use proper French accents: é, è, ê, à, ç",
        ],
    },
    "es": {
        "language": "Spanish", "formality": "formal", "script": "latin",
        "salutation": "Estimado/a Sr./Sra. [Apellido] / A quien corresponda",
        "closing": "Atentamente / Un cordial saludo",
        "checks": [
            "All text must be in Spanish",
            "Use formal 'usted' (not 'tú')",
            "Subject line must be in Spanish",
            "Use proper Spanish punctuation: ¿?, ¡!",
        ],
    },
    "pt": {
        "language": "Portuguese", "formality": "formal", "script": "latin",
        "salutation": "Prezado(a) Sr./Sra. [Nome]",
        "closing": "Atenciosamente",
        "checks": [
            "All text must be in Portuguese",
            "pt_BR: Brazilian Portuguese is slightly less formal than European",
            "Subject line must be in Portuguese",
            "Use proper accents: ã, ç, ê, ó",
        ],
    },
    "it": {
        "language": "Italian", "formality": "formal", "script": "latin",
        "salutation": "Gentile Sig./Sig.ra [Cognome] / Spettabile [Azienda]",
        "closing": "Cordiali saluti / Distinti saluti",
        "checks": [
            "All text must be in Italian",
            "Use formal 'Lei' (not 'tu')",
            "Subject line must be in Italian",
        ],
    },
    "nl": {
        "language": "Dutch", "formality": "semi-formal", "script": "latin",
        "salutation": "Geachte heer/mevrouw [Naam] / Beste [Naam]",
        "closing": "Met vriendelijke groet",
        "checks": [
            "All text must be in Dutch",
            "Dutch business culture is direct; avoid flowery language",
            "Subject line must be in Dutch",
        ],
    },
    "pl": {
        "language": "Polish", "formality": "formal", "script": "latin",
        "salutation": "Szanowny Panie/Szanowna Pani [Nazwisko] / Szanowni Państwo",
        "closing": "Z poważaniem",
        "checks": [
            "All text must be in Polish",
            "Use formal address forms",
            "Subject line must be in Polish",
            "Use proper Polish characters: ą, ć, ę, ł, ń, ó, ś, ź, ż",
        ],
    },
    "ru": {
        "language": "Russian", "formality": "formal", "script": "cyrillic",
        "salutation": "Уважаемый/Уважаемая [Имя Отчество] / Уважаемые господа",
        "closing": "С уважением",
        "checks": [
            "All text must be in Russian using Cyrillic script",
            "Use formal address with name + patronymic if known",
            "Subject line must be in Russian",
        ],
    },
    "ja": {
        "language": "Japanese", "formality": "formal", "script": "japanese",
        "salutation": "株式会社[会社名] [部署] [役職] [氏名]様",
        "closing": "よろしくお願いいたします",
        "checks": [
            "All text must be in Japanese (kanji, hiragana, katakana as appropriate)",
            "Start with 'お世話になっております' for existing contacts",
            "Use keigo (敬語) — formal honorific language throughout",
            "Subject line must be in Japanese",
            "End with 以上、よろしくお願いいたします",
        ],
    },
    "ko": {
        "language": "Korean", "formality": "formal", "script": "hangul",
        "salutation": "[회사명] [직함] [성함] 귀중 / 안녕하십니까",
        "closing": "감사합니다 / 잘 부탁드립니다",
        "checks": [
            "All text must be in Korean (Hangul)",
            "Use formal speech level (합쇼체)",
            "Subject line must be in Korean",
        ],
    },
    "zh": {
        "language": "Chinese (Simplified)", "formality": "formal", "script": "chinese_simplified",
        "salutation": "尊敬的[姓名/职位]：/ 您好",
        "closing": "此致 敬礼 / 期待您的回复",
        "checks": [
            "All text must be in Simplified Chinese",
            "Use formal business Chinese (商务中文)",
            "Subject line must be in Chinese",
            "Use 贵公司 when referring to the recipient's company",
        ],
    },
    "tw": {
        "language": "Chinese (Traditional)", "formality": "formal", "script": "chinese_traditional",
        "salutation": "敬啟者 / 您好",
        "closing": "敬祝 商祺",
        "checks": [
            "All text must be in Traditional Chinese",
            "Use formal business Chinese",
            "Subject line must be in Traditional Chinese",
        ],
    },
    "ar": {
        "language": "Arabic", "formality": "formal", "script": "arabic",
        "salutation": "السيد/السيدة [الاسم] المحترم/المحترمة / تحية طيبة وبعد",
        "closing": "مع خالص التقدير والاحترام",
        "checks": [
            "All text must be in Arabic (right-to-left)",
            "Use Modern Standard Arabic (فصحى) for business",
            "Subject line must be in Arabic",
            "Open with Islamic greeting if appropriate: السلام عليكم",
        ],
    },
    "tr": {
        "language": "Turkish", "formality": "formal", "script": "latin",
        "salutation": "Sayın [Ad Soyad] / Sayın Yetkili",
        "closing": "Saygılarımla",
        "checks": [
            "All text must be in Turkish",
            "Use formal address 'Sayın'",
            "Subject line must be in Turkish",
            "Use proper Turkish characters: ç, ğ, ı, ö, ş, ü",
        ],
    },
    "en": {
        "language": "English", "formality": "professional", "script": "latin",
        "salutation": "Dear [Name] / Dear Sir/Madam",
        "closing": "Best regards / Kind regards",
        "checks": [
            "Professional business English",
            "Clear and concise sentences",
            "Subject line should be specific and compelling",
        ],
    },
}

# Country code → locale mapping
_COUNTRY_LOCALE_MAP = {
    "de": "de_DE", "at": "de_AT", "ch": "de_CH",
    "fr": "fr_FR", "be": "fr_BE",
    "es": "es_ES", "mx": "es_MX",
    "pt": "pt_PT", "br": "pt_BR",
    "it": "it_IT",
    "nl": "nl_NL",
    "ja": "ja_JP", "jp": "ja_JP",
    "ko": "ko_KR", "kr": "ko_KR",
    "zh": "zh_CN", "cn": "zh_CN", "tw": "zh_TW",
    "ru": "ru_RU",
    "sa": "ar_SA", "ae": "ar_AE",
    "tr": "tr_TR",
    "pl": "pl_PL",
    "cz": "cs_CZ", "ro": "ro_RO", "hu": "hu_HU",
    "ua": "uk_UA",
    "se": "sv_SE", "no": "nb_NO", "dk": "da_DK", "fi": "fi_FI",
    "th": "th_TH", "vn": "vi_VN", "id": "id_ID",
    "in": "hi_IN", "gr": "el_GR",
}

# ── Sequence step definitions (single source of truth for N-email support) ──
# Each step: sequence_number, email_type, suggested_send_day, objective.
# `Settings.email_sequence_steps` (1-3) decides how many of these are used.
# Default is 1 (single outreach email). Bump the setting to re-enable a
# multi-step cadence; the step table below is the source of truth.
_EMAIL_STEP_SPECS: list[dict[str, Any]] = [
    {
        "sequence_number": 1,
        "email_type": "company_intro",
        "suggested_send_day": 0,
        "objective": "establish relevance and open the conversation with a low-friction CTA.",
    },
    {
        "sequence_number": 2,
        "email_type": "product_showcase",
        "suggested_send_day": 3,
        "objective": "deepen relevance using product/application fit or proof points.",
    },
    {
        "sequence_number": 3,
        "email_type": "partnership_proposal",
        "suggested_send_day": 7,
        "objective": "polite follow-up that probes distributor/buyer fit without pressure.",
    },
]


def _configured_sequence_steps() -> int:
    """Number of emails per sequence from settings, clamped to 1..3."""
    try:
        return max(1, min(3, int(getattr(get_settings(), "email_sequence_steps", 1) or 1)))
    except Exception:
        return 1


def _active_step_specs(steps: int | None = None) -> list[dict[str, Any]]:
    if steps is None:
        steps = _configured_sequence_steps()
    return _EMAIL_STEP_SPECS[: max(1, min(3, int(steps or 1)))]


def _personalize_per_lead_enabled(settings: Any = None) -> bool:
    """Whether every lead gets its own independent draft (no template reuse)."""
    if settings is None:
        settings = get_settings()
    value = getattr(settings, "email_personalize_per_lead", True)
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return True


# Placeholder tokens the LLM likes to leave in closings when it doesn't
# know the sender's name. The validator flags them and the rewrite loop
# replaces them with the configured signature.
_PLACEHOLDER_TOKENS = (
    "[your name]",
    "[your company]",
    "[your company name]",
    "[name]",
    "[full name]",
    "[signature]",
    "[sender name]",
    "[subject]",
    "[company name]",
)


# Defensive localization: the locale validator prompt instructs the LLM to
# write issues/suggestions in Simplified Chinese, but some models still
# respond in English. These two functions catch the most common patterns
# so the user-facing UI stays in Chinese.
_VALIDATOR_PHRASE_MAP: tuple[tuple[str, str], ...] = (
    # Email N → 第 N 封 (sentence-start or after punctuation)
    (r"\bEmail\s*([123])\b", r"第 \1 封"),
    # Common validator phrases — order matters: more specific first
    (r"\b(?:grammar|grammatical)\s+(?:issue|error|problem)s?\b", "语法问题"),
    (r"\bgrammatical(?:ly)?\b", "语法上"),
    (r"\bgrammar\b", "语法"),
    (r"\bspelling\s+(?:issue|error|problem)s?\b", "拼写问题"),
    (r"\bspelling\b", "拼写"),
    (r"\bsubject line\b", "主题行"),
    (r"\bsubject\b", "主题"),
    (r"\bclosing statement\b", "结尾段落"),
    (r"\bcall to action\b", "行动号召"),
    (r"\bCTA\b", "行动号召"),
    (r"\bbuyer[- ]oriented\b", "面向买方的"),
    (r"\bproof points?\b", "佐证要点"),
    (r"\bgeneric (marketing )?claims?\b", "泛化营销话术"),
    (r"\btoo short\b", "过短"),
    (r"\btoo long\b", "过长"),
    (r"\btoo aggressive\b", "过于激进"),
    (r"\btoo generic\b", "过于泛化"),
    (r"\brepeats?\b", "重复"),
    (r"\bcompelling\b", "有吸引力"),
    (r"\bspecific\b", "具体"),
    (r"\bbrief\b", "简洁"),
    (r"\bconcrete\b", "具体"),
    (r"\brevise\b", "修改"),
    (r"\benhance\b", "加强"),
    (r"\bsalutation\b", "称呼"),
    (r"\bformality\b", "语气"),
    (r"\bclarity\b", "清晰度"),
    (r"\bnaturalness\b", "自然度"),
)


def _localize_validator_text(text: str) -> str:
    """Best-effort fallback to convert an English validator string to Chinese.

    The locale validator prompt tells the LLM to write in Simplified
    Chinese, but not all models comply. When that happens this function
    applies a small set of regex replacements so the user-facing UI stays
    readable.
    """
    if not text:
        return text
    out = str(text)
    for pattern, replacement in _VALIDATOR_PHRASE_MAP:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    return out


def _localize_validator_list(items: list[str] | None) -> list[str]:
    if not items:
        return list(items or [])
    return [_localize_validator_text(str(item)) for item in items]


def _sender_signature(settings: Any = None) -> str:
    """The exact closing signature emails must be signed with.

    Prefers `EMAIL_SIGNATURE_BLOCK` (free-form, may be multi-line, e.g.
    name + title + company), falls back to `EMAIL_FROM_NAME`. Injected
    into the craft prompt so the model stops emitting `[Your Name]`.
    """
    if settings is None:
        settings = get_settings()
    block = str(getattr(settings, "email_signature_block", "") or "").strip()
    if block:
        return block
    name = str(getattr(settings, "email_from_name", "") or "").strip()
    if name and not name.lower().startswith("<magicmock"):
        return name
    return "Ai Hunter"


# ── ReAct system prompt ───────────────────────────────────────────────────────
def _build_react_system(steps: int | None = None) -> str:
    """Build the ReAct system prompt for a sequence of `steps` emails."""
    specs = _active_step_specs(steps)
    n = len(specs)
    task_noun = "a single outreach email" if n == 1 else f"a {n}-email outreach sequence"
    draft_target = "the email" if n == 1 else f"all {n} emails"
    schema_entries = ",\n".join(
        "    {\n"
        f"      \"sequence_number\": {spec['sequence_number']},\n"
        f"      \"email_type\": \"{spec['email_type']}\",\n"
        "      \"subject\": \"...\",\n"
        "      \"body_text\": \"...\",\n"
        f"      \"suggested_send_day\": {spec['suggested_send_day']},\n"
        "      \"personalization_points\": [\"...\"],\n"
        "      \"cultural_adaptations\": [\"...\"]\n"
        "    }"
        for spec in specs
    )
    return f"""You are an expert B2B email copywriter specialising in international business communication.

Your task: write {task_noun} for a potential buyer/distributor, then validate and refine it.

## Workflow (follow this order):
1. THINK — analyse the lead profile, locale, industry, and cultural context.
2. DRAFT — write {draft_target}.
3. VALIDATE — call validate_emails with your draft JSON to check language, formality, salutation, and cultural fit.
4. REVISE — if validation returns issues, fix them and call validate_emails again (max 2 validation rounds).
5. OUTPUT — once validation passes, output the final JSON.

## Final output MUST be this exact JSON structure:
{{
  "locale": "<locale>",
  "emails": [
{schema_entries}
  ]
}}

## Rules:
1. Write ALL subject and body_text in the target locale language.
2. Adapt tone, formality, salutation, and closing to the target culture.
3. Each email: 100-200 words.
4. Personalise based on the lead's industry and description.
5. Include specific product references.
6. Use proper plain-text email layout:
   - salutation line
   - blank line
   - 2-3 short body paragraphs
   - blank line
   - closing line
   - blank line
   - sender signature (use the signature given in the prompt EXACTLY)
7. Every email MUST be a complete, self-contained message. The body
   must end with a localised closing line (e.g. "Best regards",
   "Mit freundlichen Grüßen", "此致敬礼", "よろしくお願いいたします")
   followed by the sender signature. NEVER truncate the body mid-sentence
   or end with a dangling clause — a missing closing is a hard fail.
8. Close every email with the sender signature given in the prompt. NEVER
   emit placeholder tokens such as [Your Name], [Name], [Your Company] —
   if a signature is provided, sign with it exactly.
9. Output ONLY the JSON object — no extra text.
10. **Product hook is mandatory.** Each email MUST anchor on at least
    one concrete attribute of *our* product (a specific model, spec,
    certification, or named customer reference drawn from "## Your
    Company"). Generic claims ("high quality", "competitive pricing",
    "industry-leading", "global presence", "best-in-class") without
    backing detail count as a template failure and will be rejected.
    Pull a specific value angle from the Strategy Brief, not boilerplate.
11. The recipient's business must appear with at least one concrete
    detail (a real product line, market, or website reference from
    "## Target Lead") so the email is unmistakably addressed to *them*,
    not a generic audience."""


# Backwards-compatible constant (default 1-email variant). Runtime
# paths call `_build_react_system()` directly with the resolved
# step count, so `Settings.email_sequence_steps` takes effect.
EMAIL_REACT_SYSTEM = _build_react_system(1)


EMAIL_FEWSHOT_EXAMPLES = """
## Example A — English, distributor outreach
{
  "locale": "en_US",
  "emails": [
    {
      "sequence_number": 1,
      "email_type": "company_intro",
      "subject": "Potential fit for your industrial components range",
      "body_text": "Dear Mr. Carter, I noticed your company supplies industrial electrical components to OEM and maintenance customers. We manufacture switch components used in appliance and control-system applications, and your product mix suggests there could be relevance. If this category is of interest, I can send a short overview of the most relevant models and certifications. Best regards,",
      "suggested_send_day": 0,
      "personalization_points": ["industrial components range"],
      "cultural_adaptations": ["professional English", "low-friction CTA"]
    }
  ]
}

## Example B — German, formal buyer outreach
{
  "locale": "de_DE",
  "emails": [
    {
      "sequence_number": 1,
      "email_type": "company_intro",
      "subject": "Mögliche Relevanz für Ihr Sortiment im Bereich Schaltkomponenten",
      "body_text": "Sehr geehrte Damen und Herren, Ihrem Unternehmensprofil nach beliefern Sie industrielle Kunden mit elektrischen Komponenten. Wir fertigen Schalter und verwandte Baugruppen für Haushaltsgeräte und Steuerungssysteme. Daher könnte es Berührungspunkte mit Ihrem Sortiment geben. Wenn das Thema für Sie relevant ist, sende ich Ihnen gern eine kurze Übersicht der passenden Modelle und Zertifizierungen. Mit freundlichen Grüßen,",
      "suggested_send_day": 0,
      "personalization_points": ["industrielle Kunden", "elektrische Komponenten"],
      "cultural_adaptations": ["formal German salutation", "direct but polite CTA"]
    }
  ]
}

## Example C — Simplified Chinese, business style
{
  "locale": "zh_CN",
  "emails": [
    {
      "sequence_number": 1,
      "email_type": "company_intro",
      "subject": "或许与贵司现有产品线相关的开关器件",
      "body_text": "您好，从贵司公开资料来看，贵司在工业电气/配套器件领域具备较强的分销与供货能力。我们主要生产微动开关及相关组件，适用于家电和控制系统场景，因此与贵司现有业务可能存在一定匹配度。如您方便，我可以先发一版精简的产品与认证信息，供贵司初步评估。期待您的回复。",
      "suggested_send_day": 0,
      "personalization_points": ["工业电气", "分销与供货能力"],
      "cultural_adaptations": ["formal business Chinese", "polite low-pressure CTA"]
    }
  ]
}
"""


LANGUAGE_SELECTOR_SYSTEM = """You are a B2B email language-routing specialist.

Choose the best language for an outbound business email.

Priority:
1. Maximise the chance the recipient can read and respond comfortably.
2. Prefer the language clearly evidenced by the lead's public-facing communication.
3. If evidence is weak or mixed, prefer English.
4. Do not force a local language only because of country if business evidence suggests English is safer.

Return JSON only:
{
  "chosen_language": "...",
  "chosen_locale": "...",
  "confidence": "high|medium|low",
  "reason": "...",
  "fallback_used": true
}"""


BRIEF_SYNTHESIS_SYSTEM = """You are a B2B outbound strategist.

Do not write the emails yet.
Prepare a concise strategy brief for the configured N-step outbound sequence
(default: a single outreach email; the operator may opt into a multi-step
cadence via Settings.email_sequence_steps).

The brief must be grounded in actual facts from:
- the seller's products, positioning, and ICP
- the buyer's industry, website, profile, and lead evidence

Do not invent facts.
Do not use vague generic value propositions unless directly supported.
Prefer concrete buyer relevance over generic sales language.

Return JSON only."""


EMAIL_REWRITER_SYSTEM = """You are an expert international B2B email editor.

You will receive:
- the target locale and language requirements
- the current email sequence JSON
- a list of validation issues and rewrite instructions

Your task is to revise the sequence so it:
- fixes every issue
- preserves factual accuracy
- remains natural for the local business culture
- keeps the progression between emails distinct (when there is more than one)
- changes only the minimum text necessary to fix the listed issues
- preserves any part of the sequence that already works
- does not invent claims, certifications, customers, or performance statements
- keeps valid personalization, CTA intent, and send-day progression unless a listed issue requires changing them
- keeps a real plain-text email layout with visible paragraph breaks instead of one dense block

Return JSON only in the same schema as the original sequence."""


EMAIL_TEMPLATE_PERSONALIZER_SYSTEM = """You rewrite an outbound email so it is genuinely tailored to one specific target company.

You will receive:
- the source lead the draft was originally written for
- the target lead and recipient details (company, website, industry, description, contact)
- the reusable template profile and template plan
- the current draft email sequence

Your task:
- rewrite the email BODY so it speaks to the target company's actual business:
  reference their product range, distribution/wholesale role, market, or
  website details from the lead profile — not just their company name
- keep the same tone, CTA style, locale, and plain-text layout
- replace ALL source-lead-specific references with accurate target-lead details
- preserve factual accuracy and avoid inventing claims
- keep the email structure (number of emails, sequence_number, email_type,
  suggested_send_day) unchanged
- keep the sender signature/closing exactly as provided — never introduce
  placeholders like [Your Name] or [Your Company]

Return JSON only in the same schema as the original sequence."""


def _get_locale(country_code: str) -> str:
    """Map country code to locale. Default to en_US."""
    return _COUNTRY_LOCALE_MAP.get(country_code.lower(), "en_US")


def _get_locale_rules(locale: str) -> dict[str, Any]:
    """Get validation rules for a locale. Falls back to English rules."""
    parts = locale.lower().split("_")
    lang = parts[0]
    country = parts[1] if len(parts) > 1 else ""
    if lang == "zh" and country == "tw":
        return _LOCALE_RULES.get("tw", _LOCALE_RULES["en"])
    return _LOCALE_RULES.get(lang, _LOCALE_RULES["en"])


def _locale_for_language(language_code: str, fallback_locale: str = "en_US") -> str:
    normalized = str(language_code or "").strip().lower()
    if not normalized:
        return fallback_locale
    mapping = {
        "en": "en_US",
        "de": "de_DE",
        "fr": "fr_FR",
        "es": "es_ES",
        "pt": "pt_PT",
        "it": "it_IT",
        "nl": "nl_NL",
        "pl": "pl_PL",
        "ru": "ru_RU",
        "ja": "ja_JP",
        "ko": "ko_KR",
        "zh": "zh_CN",
        "zh-cn": "zh_CN",
        "zh-tw": "zh_TW",
        "ar": "ar_SA",
        "tr": "tr_TR",
    }
    return mapping.get(normalized, fallback_locale)


def _slugify_template_segment(value: str, fallback: str = "general") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return normalized or fallback


def _required_tokens_for_template(
    template_profile: dict[str, Any] | None,
    settings: Any,
) -> list[str]:
    """Resolve the active set of required tokens for template adherence.

    Priority:
      1. ``email_template_required_tokens_override`` setting (manual,
         comma-separated). Useful when the operator knows the exact
         phrases they want preserved.
      2. ``template_profile["required_tokens"]`` (auto-extracted by
         ``extract_required_tokens`` from the user examples).
      3. Empty list — no adherence check.

    Returns the deduped list, order preserved.
    """
    override = str(getattr(settings, "email_template_required_tokens_override", "") or "").strip()
    if override:
        manual = [token.strip() for token in override.split(",") if token.strip()]
        if manual:
            return manual
    if not isinstance(template_profile, dict):
        return []
    raw = template_profile.get("required_tokens") or []
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())
        out.append(token)
    return out


def _email_token_match_ratio(body_text: str, required_tokens: list[str]) -> tuple[float, list[str]]:
    """Return ``(matched_ratio, missing_tokens)`` for a body against
    the required-token list. A token matches if it appears anywhere
    in ``body_text`` (case-insensitive substring)."""
    if not required_tokens:
        return 1.0, []
    body = (body_text or "").lower()
    missing: list[str] = []
    hits = 0
    for token in required_tokens:
        if str(token or "").strip().lower() in body:
            hits += 1
        else:
            missing.append(token)
    return (hits / len(required_tokens)) if required_tokens else 1.0, missing


def _fallback_email_from_template(
    raw_template_example: str,
    *,
    lead: dict[str, Any],
    target: dict[str, str],
    fallback_subject: str,
    fallback_locale: str,
) -> dict[str, Any]:
    """Last-resort template adherence: build a single email from the
    user's raw example by replacing buyer- and contact-specific
    placeholders. Used when the LLM keeps drifting away from the
    template after one revision attempt.

    Placeholders we recognise (case-insensitive):
      {company_name} {industry} {contact_name} {contact_title}
    Anything else stays as-is so the user can still recognise their
    own template voice in the output.
    """
    text = str(raw_template_example or "").strip()
    if not text:
        return {
            "subject": fallback_subject,
            "body_text": "",
            "suggested_send_day": 0,
            "email_type": "introduction",
            "personalization_points": [],
            "_template_fallback": True,
        }
    subject_line = fallback_subject or "Quick note"
    body_text = text
    lead_company = str(lead.get("company_name", "") or "").strip()
    lead_industry = str(lead.get("industry", "") or "").strip()
    target_name = str(target.get("target_name", "") or "").strip()
    target_title = str(target.get("target_title", "") or "").strip()

    # Heuristic split: first non-empty line is the subject, rest is body.
    # The user examples may or may not carry a subject line. Try to
    # detect it via the first short line.
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and len(lines[0]) <= 80 and not lines[0].lower().startswith(("hi ", "dear ", "hello", "hey ")):
        subject_line = lines[0].strip()
        body_text = "\n".join(lines[1:]).strip() or text

    replacements: list[tuple[str, str]] = []
    for needle, value in (
        ("{company_name}", lead_company),
        ("{industry}", lead_industry),
        ("{contact_name}", target_name),
        ("{contact_title}", target_title),
    ):
        if value and needle.lower() in body_text.lower():
            # Case-preserving replace: we only know the lowercase
            # version of the needle, so do a global case-insensitive
            # replace via regex.
            body_text = re.sub(re.escape(needle), value, body_text, flags=re.IGNORECASE)
    for needle, value in (
        ("{company_name}", lead_company),
        ("{industry}", lead_industry),
    ):
        if value and needle.lower() in subject_line.lower():
            subject_line = re.sub(re.escape(needle), value, subject_line, flags=re.IGNORECASE)

    return {
        "subject": subject_line,
        "body_text": body_text,
        "suggested_send_day": 0,
        "email_type": "introduction",
        "personalization_points": [p for p in (lead_company, lead_industry, target_title) if p],
        "_template_fallback": True,
        "locale": fallback_locale,
    }


def _build_raw_template_fallback(
    raw_examples: list[str],
    *,
    lead: dict[str, Any],
    target: dict[str, str],
    step_specs: list[dict[str, Any]],
    locale: str,
) -> list[dict[str, Any]]:
    """Materialize a full N-step sequence from raw user examples by
    mapping each step to a (possibly truncated) example. Used when
    the LLM output fails the template-adherence check and we want to
    fall back to the user's literal voice.
    """
    if not raw_examples:
        return []
    sequence: list[dict[str, Any]] = []
    for index, spec in enumerate(step_specs):
        example = raw_examples[index % len(raw_examples)]
        if not example:
            continue
        day = int(spec.get("suggested_send_day", index * 3) or 0)
        email = _fallback_email_from_template(
            example,
            lead=lead,
            target=target,
            fallback_subject=spec.get("objective", "Quick note"),
            fallback_locale=locale,
        )
        email["suggested_send_day"] = day
        email["email_type"] = spec.get("email_type", email.get("email_type", "introduction"))
        sequence.append(email)
    return sequence


def _derive_template_group(
    lead: dict[str, Any],
    *,
    target: dict[str, str],
    locale: str,
) -> str:
    target_type = _slugify_template_segment(target.get("target_type", ""), "contact")
    industry = _slugify_template_segment(lead.get("industry", ""), "general")
    return f"{locale}|{target_type}|{industry}"


def _template_id_for_group(group_key: str) -> str:
    digest = hashlib.sha1(group_key.encode("utf-8")).hexdigest()[:12]
    return f"tpl_{digest}"


def _template_version_group(group_key: str, version_index: int) -> str:
    return f"{group_key}|v{max(1, int(version_index or 1))}"


def _replace_template_tokens(
    value: str,
    *,
    source_lead: dict[str, Any],
    target_lead: dict[str, Any],
    source_target: dict[str, str],
    target_target: dict[str, str],
) -> str:
    updated = str(value or "")
    replacements = [
        (str(source_lead.get("company_name", "") or ""), str(target_lead.get("company_name", "") or "")),
        (str(source_lead.get("industry", "") or ""), str(target_lead.get("industry", "") or "")),
        (str(source_target.get("target_name", "") or ""), str(target_target.get("target_name", "") or "")),
        (str(source_target.get("target_title", "") or ""), str(target_target.get("target_title", "") or "")),
        (str(source_target.get("target_email", "") or ""), str(target_target.get("target_email", "") or "")),
    ]
    for source_value, target_value in replacements:
        if source_value and target_value and source_value in updated:
            updated = updated.replace(source_value, target_value)
    return updated


def _apply_template_to_lead(
    template_result: dict[str, Any],
    *,
    lead: dict[str, Any],
    target: dict[str, str],
    template_group: str,
    template_index: int,
    template_assigned_count: int,
    template_max_send_count: int,
) -> dict[str, Any]:
    cloned = copy.deepcopy(template_result)
    source_lead = cloned.get("lead", {})
    source_target = cloned.get("target", {})
    template_id = _template_id_for_group(template_group)
    adapted_emails: list[dict[str, Any]] = []

    for email in cloned.get("emails", []):
        updated_email = copy.deepcopy(email)
        updated_email["subject"] = _replace_template_tokens(
            updated_email.get("subject", ""),
            source_lead=source_lead,
            target_lead=lead,
            source_target=source_target,
            target_target=target,
        )
        updated_email["body_text"] = _replace_template_tokens(
            updated_email.get("body_text", ""),
            source_lead=source_lead,
            target_lead=lead,
            source_target=source_target,
            target_target=target,
        )
        points = list(updated_email.get("personalization_points", []))
        lead_company = str(lead.get("company_name", "") or "").strip()
        lead_industry = str(lead.get("industry", "") or "").strip()
        target_title = str(target.get("target_title", "") or "").strip()
        if lead_company and lead_company not in points:
            points.append(lead_company)
        if lead_industry and lead_industry not in points:
            points.append(lead_industry)
        if target_title and target_title not in points:
            points.append(target_title)
        updated_email["personalization_points"] = points
        adapted_emails.append(updated_email)

    cloned["lead"] = lead
    cloned["target"] = target
    cloned["emails"] = adapted_emails
    cloned["template_group"] = template_group
    cloned["template_id"] = template_id
    cloned["template_usage_index"] = template_index
    cloned["generation_mode"] = "template_pool"
    cloned["template_reused"] = template_index > 1
    cloned["template_max_send_count"] = template_max_send_count
    cloned["template_assigned_count"] = template_assigned_count
    cloned["template_remaining_capacity"] = max(template_max_send_count - template_assigned_count, 0)
    cloned["template_performance"] = {
        "sent_count": 0,
        "replied_count": 0,
        "reply_rate": 0.0,
        "status": "warming_up",
    }
    return cloned


async def _personalize_template_sequence(
    llm: LLMTool,
    *,
    base_sequence: dict[str, Any],
    lead: dict[str, Any],
    target: dict[str, str],
    insight: dict[str, Any],
) -> dict[str, Any] | None:
    source_lead = base_sequence.get("lead", {}) or {}
    source_target = base_sequence.get("target", {}) or {}
    locale = str(base_sequence.get("locale", "en_US") or "en_US")
    template_profile = base_sequence.get("template_profile", {}) or {}
    template_plan = base_sequence.get("template_plan", {}) or {}
    strategy_brief = base_sequence.get("strategy_brief", {}) or {}
    prompt = (
        f"<locale>\n{locale}\n</locale>\n\n"
        f"<seller>\n{json.dumps(insight, ensure_ascii=False)}\n</seller>\n\n"
        f"<template_profile>\n{json.dumps(template_profile, ensure_ascii=False)}\n</template_profile>\n\n"
        f"<template_plan>\n{json.dumps(template_plan, ensure_ascii=False)}\n</template_plan>\n\n"
        f"<strategy_brief>\n{json.dumps(strategy_brief, ensure_ascii=False)}\n</strategy_brief>\n\n"
        f"<source_lead>\n{json.dumps(source_lead, ensure_ascii=False)}\n</source_lead>\n\n"
        f"<source_target>\n{json.dumps(source_target, ensure_ascii=False)}\n</source_target>\n\n"
        f"<target_lead>\n{json.dumps(lead, ensure_ascii=False)}\n</target_lead>\n\n"
        f"<target_recipient>\n{json.dumps(target, ensure_ascii=False)}\n</target_recipient>\n\n"
        f"<seed_sequence>\n{json.dumps(base_sequence.get('emails', []), ensure_ascii=False)}\n</seed_sequence>\n\n"
        f"Adapt the seed sequence for the target lead. Preserve the sequence structure and locale, "
        f"but make the buyer relevance and personalization specific to the target lead."
    )
    try:
        raw = await llm.generate(
            prompt,
            system=EMAIL_TEMPLATE_PERSONALIZER_SYSTEM,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        if not isinstance(raw, str):
            return None
        parsed = parse_json(raw, context="email_template_personalizer")
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        logger.debug("[EmailCraft] Template personalization failed for %s: %s", lead.get("company_name"), exc)
    return None


def _rule_validate_emails_payload(
    emails_list: list[dict[str, Any]],
    steps: int | None = None,
) -> dict[str, Any]:
    # The size check ("序列需要恰好包含 N 封") is intentionally omitted:
    # the LLM is free to ship 1, 2, or N emails per sequence. The
    # per-email + cross-step checks below still run on whatever the
    # LLM produced.
    issues: list[str] = []
    suggestions: list[str] = []

    specs = _active_step_specs(steps)
    expected = [(spec["email_type"], spec["suggested_send_day"]) for spec in specs]
    previous_subject = ""
    for i, em in enumerate(emails_list):
        etype, eday = expected[i] if i < len(expected) else (None, None)
        if etype is not None and em.get("email_type") != etype:
            issues.append(f"第 {i + 1} 封：email_type 应为 '{etype}'，当前为 '{em.get('email_type')}'")
        if em.get("suggested_send_day") != eday:
            issues.append(f"第 {i + 1} 封：suggested_send_day 应为 {eday}")
        if not str(em.get("subject", "") or "").strip():
            issues.append(f"第 {i + 1} 封：主题为空")
        body = str(em.get("body_text", "") or "")
        formatted_body = format_plaintext_email_body(body)
        wc = len(body.split())
        if wc < 50:
            issues.append(f"第 {i + 1} 封：正文过短（{wc} 词，至少 50 词）")
            suggestions.append(f"第 {i + 1} 封：扩写至 100-200 词")
        elif wc > 300:
            issues.append(f"第 {i + 1} 封：正文过长（{wc} 词，最多 300 词）")
        if wc >= 50 and "\n\n" not in formatted_body:
            issues.append(f"第 {i + 1} 封：纯文本缺少段落分隔")
            suggestions.append(f"第 {i + 1} 封：使用问候语、2-3 个短段落，并单独一行收尾")

        lowered_body = body.lower()
        lowered_subject = str(em.get("subject", "") or "").lower()
        placeholder_hits = [t for t in _PLACEHOLDER_TOKENS if t in lowered_body or t in lowered_subject]
        if placeholder_hits:
            issues.append(
                f"第 {i + 1} 封：包含占位文本（{', '.join(placeholder_hits)}）"
            )
            suggestions.append(
                f"第 {i + 1} 封：请用 prompt 中真实的发件人签名替换占位"
            )
        if not any(token in lowered_body for token in ["you", "your", "您", "贵公司", "votre", "ihr", "su ", "sua ", "vos", "tu empresa"]):
            issues.append(f"第 {i + 1} 封：缺少面向买方的明确表达")
            suggestions.append(f"第 {i + 1} 封：说明此收件人/公司为何值得联系")
        # Generic marketing claims without backing detail. We flag
        # these regardless of word count because they undercut the
        # product-hook requirement (see prompt rule #10) — even a
        # long, generic email reads as template noise.
        generic_phrases = (
            "leading provider", "world-class", "best-in-class",
            "industry-leading", "globally recognized", "world leader",
            "top-tier", "industry leader", "global leader",
            "cutting-edge technology", "state-of-the-art",
            "world-renowned", "high quality", "competitive pricing",
            "best price", "best quality", "premier provider",
            "一站式", "行业领先", "全球领先", "业界领先",
            "世界一流", "顶尖", "高品质",
        )
        if any(phrase in lowered_body for phrase in generic_phrases):
            issues.append(f"第 {i + 1} 封：依赖泛化营销话术")
            suggestions.append(
                f"第 {i + 1} 封：用具体卖点（型号 / 规格 / 认证 / 客户案例 / 数字）替换泛化吹捧"
            )
        if previous_subject and previous_subject == lowered_subject:
            issues.append(f"第 {i + 1} 封：主题与上一封重复")
        previous_subject = lowered_subject

    if len(emails_list) >= 2:
        # Cross-step checks only meaningful for multi-step sequences.
        email_1 = str(emails_list[0].get("body_text", "") or "").lower()
        email_2 = str(emails_list[1].get("body_text", "") or "").lower()
        if email_1[:120] == email_2[:120]:
            issues.append("第 2 封与第 1 封内容重复，未加深相关性")
            suggestions.append("第 2 封应补充产品/应用匹配度或具体佐证")
        if len(emails_list) >= 3:
            email_3 = str(emails_list[2].get("body_text", "") or "").lower()
            if any(token in email_3 for token in ["urgent", "last chance", "final notice"]):
                issues.append("第 3 封的 CTA 对冷启动过于激进")
                suggestions.append("第 3 封建议改为更轻量的跟进或资格确认式 CTA")

    return {
        "passed": len(issues) == 0,
        "issues": issues,
        "suggestions": suggestions,
    }


async def _locale_validate_emails_payload(
    llm: LLMTool,
    locale: str,
    emails_list: list[dict[str, Any]],
) -> dict[str, Any]:
    rules = _get_locale_rules(locale)
    lang_name = rules["language"]
    formality = rules["formality"]
    salutation = rules["salutation"]
    closing = rules["closing"]
    rule_checks = "\n".join(f"  - {c}" for c in rules["checks"])
    sample = "\n---\n".join(
        f"Email {i+1} subject: {e.get('subject','')}\nBody: {e.get('body_text','')[:700]}"
        for i, e in enumerate(emails_list[:3])
    )
    validation_prompt = (
        f"You are a {lang_name} language expert and B2B communication specialist.\n"
        f"Locale: {locale} | Language: {lang_name} | Formality: {formality}\n"
        f"Expected salutation: {salutation}\n"
        f"Expected closing: {closing}\n\n"
        f"Validation rules:\n{rule_checks}\n\n"
        f"Emails to validate:\n{sample}\n\n"
        f"Check each email against ALL rules. Also verify: grammar, spelling, punctuation, local business naturalness, tone, buyer relevance, concrete seller value, low-friction CTA, and whether the 3 emails progress instead of repeating.\n"
        f"Return JSON:\n"
        f'{{"passed": true/false, "grammar_ok": true/false, "spelling_ok": true/false, "language_correct": true/false, '
        f'"formality_correct": true/false, "salutation_correct": true/false, "business_etiquette_ok": true/false, '
        f'"local_naturalness_ok": true/false, "commercial_quality": true/false, "sequence_progression": true/false, '
        f'"issues": ["..."], "suggestions": ["..."]}}\n'
        f"Be specific: mention which email number has which problem.\n"
        f"\n"
        f"CRITICAL OUTPUT LANGUAGE: All string values inside the JSON — especially the 'issues' and "
        f"'suggestions' arrays — MUST be written in Simplified Chinese (简体中文), regardless of the "
        f"language of the emails being validated. Reference each email as '第 N 封' (e.g. 第 1 封, 第 2 封, 第 3 封)."
    )
    try:
        raw = await llm.generate(
            validation_prompt,
            system=f"You are a strict {lang_name} language and B2B communication validator. Return only JSON.",
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        if not isinstance(raw, str):
            raise TypeError("locale validator returned non-string output")
        parsed = parse_json(raw, context="locale_validate_emails")
        if isinstance(parsed, dict):
            # Defensive: if the LLM still returns English issues/suggestions
            # (prompt not always followed), run the phrase-level fallback
            # so the UI stays in Chinese.
            parsed["issues"] = _localize_validator_list(parsed.get("issues"))
            parsed["suggestions"] = _localize_validator_list(parsed.get("suggestions"))
            return parsed
    except Exception as exc:
        logger.debug("[EmailCraft] Locale validator failed for %s: %s", locale, exc)
    return {
        "passed": True,
        "grammar_ok": True,
        "spelling_ok": True,
        "language_correct": True,
        "formality_correct": True,
        "salutation_correct": True,
        "business_etiquette_ok": True,
        "local_naturalness_ok": True,
        "commercial_quality": True,
        "sequence_progression": True,
        "issues": [],
        "suggestions": [],
    }


async def _rewrite_email_sequence(
    llm: LLMTool,
    *,
    locale: str,
    rules: dict[str, Any],
    user_prompt: str,
    current_sequence: dict[str, Any],
    issues: list[str],
    suggestions: list[str],
) -> dict[str, Any] | None:
    email_count = len(current_sequence.get("emails", []) or []) or _configured_sequence_steps()
    rewrite_prompt = (
        f"<locale>\n"
        f"locale: {locale}\n"
        f"language: {rules['language']}\n"
        f"formality: {rules['formality']}\n"
        f"salutation: {rules['salutation']}\n"
        f"closing: {rules['closing']}\n"
        f"</locale>\n\n"
        f"<context>\n{user_prompt}\n</context>\n\n"
        f"<current_sequence>\n{json.dumps(current_sequence, ensure_ascii=False)}\n</current_sequence>\n\n"
        f"<issues>\n{json.dumps(issues, ensure_ascii=False)}\n</issues>\n\n"
        f"<rewrite_instructions>\n{json.dumps(suggestions, ensure_ascii=False)}\n</rewrite_instructions>\n\n"
        f"<hard_constraints>\n"
        f"- Keep exactly {email_count} email(s).\n"
        f"- Preserve sequence_number, email_type, and suggested_send_day unless an issue explicitly requires changing them.\n"
        f"- Keep correct personalization that is already present.\n"
        f"- Make the minimum necessary edits instead of rewriting everything.\n"
        f"- Keep each email commercially specific and natural in {rules['language']}.\n"
        f"</hard_constraints>"
    )
    try:
        raw = await llm.generate(
            rewrite_prompt,
            system=EMAIL_REWRITER_SYSTEM,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        if not isinstance(raw, str):
            return None
        parsed = parse_json(raw, context="email_rewriter")
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        logger.debug("[EmailCraft] Rewriter failed for locale %s: %s", locale, exc)
    return None


def _review_issue_requires_manual_review(issue: str) -> bool:
    normalized = str(issue or "").strip().lower()
    if not normalized:
        return False
    manual_only_markers = (
        # English (historical)
        "missing a subject",
        "missing a cta strategy",
        "missing tone guidance",
        "expected exactly 3 emails",
        # Chinese (current reviewer output).
        "缺少主题",
        "缺少 cta 策略",
        "缺少 cta",
        "缺少语气",
        "缺少语气指引",
    )
    return any(marker in normalized for marker in manual_only_markers)


def _review_allows_send(review_summary: dict[str, Any], settings: Any) -> bool:
    if not bool(getattr(settings, "email_require_approval_before_send", True)):
        return True
    return str(review_summary.get("status", "") or "") == "approved"


def _split_review_issues(
    issues: list[str],
    suggestions: list[str],
) -> tuple[list[str], list[str], list[str]]:
    manual_issues: list[str] = []
    fixable_issues: list[str] = []
    fixable_suggestions = list(dict.fromkeys(str(item) for item in suggestions if str(item).strip()))
    for issue in issues:
        cleaned = str(issue or "").strip()
        if not cleaned:
            continue
        if _review_issue_requires_manual_review(cleaned):
            manual_issues.append(cleaned)
        else:
            fixable_issues.append(cleaned)
    return manual_issues, fixable_issues, fixable_suggestions


async def _auto_improve_reviewed_sequence(
    llm: LLMTool,
    *,
    locale: str,
    rules: dict[str, Any],
    user_prompt: str,
    current_sequence: dict[str, Any],
    lead: dict[str, Any],
    template_profile: dict[str, Any],
    template_plan: dict[str, Any],
    min_score: int,
    max_blocking_issues: int,
    validation_max_revisions: int = 1,
    max_rounds: int = 2,
    required_tokens: list[str] | None = None,
    min_token_match_ratio: float = 0.5,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    current = copy.deepcopy(current_sequence)
    last_review = _review_email_sequence(
        lead,
        locale=locale,
        emails=list(current.get("emails", []) or []),
        template_profile=template_profile,
        template_plan=template_plan,
        min_score=min_score,
        max_blocking_issues=max_blocking_issues,
        required_tokens=required_tokens,
        min_token_match_ratio=min_token_match_ratio,
    )
    improvement_summary: dict[str, Any] = {
        "attempted": False,
        "rounds": 0,
        "improved": False,
        "stopped_reason": "not_needed" if last_review.get("status") == "approved" else "manual_review_required",
        "manual_review_issues": [],
        "fixable_issues": [],
    }
    if last_review.get("status") == "approved":
        return current, last_review, improvement_summary

    for _ in range(max_rounds):
        manual_issues, fixable_issues, fixable_suggestions = _split_review_issues(
            list(last_review.get("issues", []) or []),
            list(last_review.get("suggestions", []) or []),
        )
        improvement_summary["manual_review_issues"] = manual_issues
        improvement_summary["fixable_issues"] = fixable_issues
        if manual_issues:
            improvement_summary["stopped_reason"] = "manual_review_required"
            return current, last_review, improvement_summary
        if not fixable_issues:
            improvement_summary["stopped_reason"] = "no_fixable_issues"
            return current, last_review, improvement_summary

        revised = await _rewrite_email_sequence(
            llm,
            locale=locale,
            rules=rules,
            user_prompt=user_prompt,
            current_sequence=current,
            issues=fixable_issues,
            suggestions=fixable_suggestions,
        )
        if revised is None:
            improvement_summary["stopped_reason"] = "rewrite_failed"
            return current, last_review, improvement_summary

        revised, validation_summary = await _validate_and_revise_sequence(
            llm,
            locale=locale,
            rules=rules,
            user_prompt=user_prompt,
            parsed_sequence=revised,
            max_revisions=validation_max_revisions,
        )
        improvement_summary["last_validation_status"] = validation_summary.get("status", "needs_review")
        if revised is None:
            improvement_summary["stopped_reason"] = "validation_failed_after_rewrite"
            return current, last_review, improvement_summary

        validated = validate_dict(
            revised,
            EMAIL_SEQUENCE_REQUIRED,
            defaults=EMAIL_SEQUENCE_DEFAULTS,
            context="EmailCraftReviewRewriter",
        )
        if validated is None or not validated.get("emails"):
            improvement_summary["stopped_reason"] = "rewrite_invalid"
            return current, last_review, improvement_summary

        improvement_summary["attempted"] = True
        improvement_summary["rounds"] = int(improvement_summary["rounds"]) + 1
        current = validated
        next_review = _review_email_sequence(
            lead,
            locale=locale,
            emails=validated["emails"],
            template_profile=template_profile,
            template_plan=template_plan,
            min_score=min_score,
            max_blocking_issues=max_blocking_issues,
            required_tokens=required_tokens,
            min_token_match_ratio=min_token_match_ratio,
        )
        if int(next_review.get("score", 0) or 0) > int(last_review.get("score", 0) or 0):
            improvement_summary["improved"] = True
        last_review = next_review
        if last_review.get("status") == "approved":
            improvement_summary["stopped_reason"] = "approved"
            return current, last_review, improvement_summary

    improvement_summary["stopped_reason"] = "max_rounds_reached"
    return current, last_review, improvement_summary


async def _validate_and_revise_sequence(
    llm: LLMTool,
    *,
    locale: str,
    rules: dict[str, Any],
    user_prompt: str,
    parsed_sequence: dict[str, Any],
    max_revisions: int = 2,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    current = parsed_sequence
    last_summary = {
        "passed": False,
        "status": "needs_review",
        "issues": ["校验未运行"],
        "suggestions": [],
    }
    for _ in range(max_revisions + 1):
        emails_list = current.get("emails", []) if isinstance(current, dict) else []
        if not isinstance(emails_list, list) or not emails_list:
            return None, last_summary

        rule_result = _rule_validate_emails_payload(emails_list)
        locale_result = await _locale_validate_emails_payload(llm, locale, emails_list)

        issues = list(rule_result.get("issues", []))
        suggestions = list(rule_result.get("suggestions", []))
        issues.extend(locale_result.get("issues", []))
        suggestions.extend(locale_result.get("suggestions", []))

        if not locale_result.get("grammar_ok", True):
            issues.append("语法未通过：邮件存在语法问题")
        if not locale_result.get("spelling_ok", True):
            issues.append("拼写未通过：邮件存在拼写问题")
        if not locale_result.get("language_correct", True):
            issues.append(f"语言未通过：邮件未完全使用 {rules['language']}")
        if not locale_result.get("formality_correct", True):
            issues.append(f"语气未通过：应为 {rules['formality']} 语气")
        if not locale_result.get("salutation_correct", True):
            issues.append(f"称呼未通过：应为 '{rules['salutation']}'")
        if not locale_result.get("business_etiquette_ok", True):
            issues.append("商务礼仪未通过：措辞不符合当地商务邮件习惯")
        if not locale_result.get("local_naturalness_ok", True):
            issues.append("本地化自然度未通过：措辞有翻译腔或不符合当地文化")
        if not locale_result.get("commercial_quality", True):
            issues.append("商业质量未通过：序列过于泛化或对买方缺乏针对性")
        if not locale_result.get("sequence_progression", True):
            issues.append("序列递进未通过：邮件之间缺乏清晰的递进关系")

        dedup_issues = list(dict.fromkeys(str(item) for item in issues if str(item).strip()))
        dedup_suggestions = list(dict.fromkeys(str(item) for item in suggestions if str(item).strip()))

        last_summary = {
            "passed": len(dedup_issues) == 0,
            "status": "approved" if len(dedup_issues) == 0 else "needs_review",
            "issues": dedup_issues,
            "suggestions": dedup_suggestions,
        }

        if not dedup_issues:
            return current, last_summary

        revised = await _rewrite_email_sequence(
            llm,
            locale=locale,
            rules=rules,
            user_prompt=user_prompt,
            current_sequence=current,
            issues=dedup_issues,
            suggestions=dedup_suggestions,
        )
        if revised is None:
            return current, last_summary
        current = revised

    return current, last_summary


def _fallback_language_choice(
    lead: dict[str, Any],
    *,
    default_locale: str,
    language_mode: str,
    default_language: str,
    fallback_language: str,
) -> dict[str, Any]:
    website = str(lead.get("website", "") or "")
    description = str(lead.get("description", "") or "")
    target_title = str(lead.get("target_title", "") or "")
    evidence_text = f"{website} {description} {target_title}".lower()
    if language_mode == "manual":
        chosen = default_language or fallback_language or "en"
        return {
            "chosen_language": chosen,
            "chosen_locale": _locale_for_language(chosen, default_locale),
            "confidence": "high",
            "reason": "manual language mode",
            "fallback_used": chosen != default_locale.split("_")[0].lower(),
        }
    if language_mode == "english_only":
        return {
            "chosen_language": "en",
            "chosen_locale": "en_US",
            "confidence": "high",
            "reason": "english_only mode",
            "fallback_used": True,
        }
    if any(token in evidence_text for token in ["/en", "english", "global", "international"]):
        return {
            "chosen_language": "en",
            "chosen_locale": "en_US",
            "confidence": "medium",
            "reason": "public-facing evidence suggests English is safer",
            "fallback_used": True,
        }
    return {
        "chosen_language": default_locale.split("_")[0].lower(),
        "chosen_locale": default_locale,
        "confidence": "medium",
        "reason": "country/locale default",
        "fallback_used": False,
    }


async def _select_email_language(
    lead: dict[str, Any],
    target: dict[str, str],
    llm: LLMTool,
    *,
    default_locale: str,
    language_mode: str,
    default_language: str,
    fallback_language: str,
) -> dict[str, Any]:
    fallback_choice = _fallback_language_choice(
        lead,
        default_locale=default_locale,
        language_mode=language_mode,
        default_language=default_language,
        fallback_language=fallback_language,
    )
    prompt = (
        f"<settings>\n"
        f"language_mode: {language_mode}\n"
        f"default_language: {default_language}\n"
        f"fallback_language: {fallback_language}\n"
        f"</settings>\n\n"
        f"<lead>\n"
        f"company_name: {lead.get('company_name', '')}\n"
        f"website: {lead.get('website', '')}\n"
        f"description: {lead.get('description', '')}\n"
        f"country_code: {lead.get('country_code', '')}\n"
        f"contact_name: {target.get('target_name', '')}\n"
        f"contact_title: {target.get('target_title', '')}\n"
        f"</lead>\n\n"
        f"<instructions>\n"
        f"Choose the most appropriate outbound email language.\n"
        f"Use local language only when there is strong evidence it is the better business choice.\n"
        f"If uncertain, choose English.\n"
        f"</instructions>"
    )
    try:
        raw = await llm.generate(
            prompt,
            system=LANGUAGE_SELECTOR_SYSTEM,
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        if not isinstance(raw, str):
            return fallback_choice
        parsed = parse_json(raw, context="email_language_selector")
        if isinstance(parsed, dict) and parsed.get("chosen_language"):
            chosen_locale = parsed.get("chosen_locale") or _locale_for_language(
                str(parsed.get("chosen_language", "")),
                default_locale,
            )
            return {
                "chosen_language": str(parsed.get("chosen_language", fallback_choice["chosen_language"])),
                "chosen_locale": str(chosen_locale),
                "confidence": str(parsed.get("confidence", fallback_choice["confidence"])),
                "reason": str(parsed.get("reason", fallback_choice["reason"])),
                "fallback_used": bool(parsed.get("fallback_used", fallback_choice["fallback_used"])),
            }
    except Exception as exc:
        logger.debug("[EmailCraft] Language selector failed for %s: %s", lead.get("company_name"), exc)
    return fallback_choice


async def _synthesise_email_brief(
    lead: dict[str, Any],
    insight: dict[str, Any],
    target: dict[str, str],
    llm: LLMTool,
) -> dict[str, Any]:
    fallback_brief = {
        "recipient_profile": str(lead.get("industry", "") or "Potential distributor or buyer"),
        "why_this_company_may_fit": [
            str(lead.get("industry", "") or "Operates in a relevant industry"),
            str(lead.get("description", "") or "Public profile suggests potential buyer relevance"),
        ],
        "best_value_angles": list((insight.get("value_propositions") or [])[:2]) or [
            "Relevant product portfolio for distributor conversations",
            "Potential long-term supply partnership",
        ],
        "product_focus": list((insight.get("products") or [])[:2]),
        "proof_points_to_use": list((insight.get("value_propositions") or [])[:2]),
        "claims_to_avoid": ["Avoid unverifiable superlatives", "Avoid generic mass-email phrasing"],
        "cta_strategy": "Ask a low-friction qualification question about category ownership or interest.",
        "tone_guidance": "Professional, concise, commercially credible.",
        "personalization_hooks": [
            str(lead.get("company_name", "") or ""),
            str(lead.get("description", "") or ""),
            str(target.get("target_title", "") or ""),
        ],
    }
    prompt = (
        f"<seller_company>\n"
        f"name: {insight.get('company_name', '')}\n"
        f"summary: {insight.get('summary', '')}\n"
        f"products: {json.dumps(insight.get('products', []), ensure_ascii=False)}\n"
        f"industries: {json.dumps(insight.get('industries', []), ensure_ascii=False)}\n"
        f"value_propositions: {json.dumps(insight.get('value_propositions', []), ensure_ascii=False)}\n"
        f"target_customer_profile: {insight.get('target_customer_profile', '')}\n"
        f"negative_targeting_criteria: {json.dumps(insight.get('negative_targeting_criteria', []), ensure_ascii=False)}\n"
        f"</seller_company>\n\n"
        f"<buyer_company>\n"
        f"company_name: {lead.get('company_name', '')}\n"
        f"website: {lead.get('website', '')}\n"
        f"industry: {lead.get('industry', '')}\n"
        f"description: {lead.get('description', '')}\n"
        f"country_code: {lead.get('country_code', '')}\n"
        f"contact_name: {target.get('target_name', '')}\n"
        f"contact_title: {target.get('target_title', '')}\n"
        f"target_email_type: {target.get('target_type', '')}\n"
        f"fit_score: {lead.get('fit_score', lead.get('match_score', ''))}\n"
        f"contactability_score: {lead.get('contactability_score', '')}\n"
        f"</buyer_company>\n\n"
        f"<task>\n"
        f"Build an outbound strategy brief.\n"
        f"Focus on why this buyer may care, which products are most relevant, what proof points are credible, and what CTA is appropriate for a first cold outreach.\n"
        f"</task>"
    )
    try:
        raw = await llm.generate(
            prompt,
            system=BRIEF_SYNTHESIS_SYSTEM,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        if not isinstance(raw, str):
            return fallback_brief
        parsed = parse_json(raw, context="email_brief_synthesizer")
        if isinstance(parsed, dict):
            merged = fallback_brief | parsed
            return merged
    except Exception as exc:
        logger.debug("[EmailCraft] Brief synthesis failed for %s: %s", lead.get("company_name"), exc)
    return fallback_brief
def _review_email_sequence(
    lead: dict[str, Any],
    *,
    locale: str,
    emails: list[dict[str, Any]],
    template_profile: dict[str, Any],
    template_plan: dict[str, Any],
    min_score: int,
    max_blocking_issues: int,
    required_tokens: list[str] | None = None,
    min_token_match_ratio: float = 0.5,
) -> dict[str, Any]:
    score = 100
    issues: list[str] = []
    suggestions: list[str] = []

    # The size check ("序列需要恰好包含 N 封") is intentionally omitted:
    # the LLM is free to ship 1, 2, or N emails per sequence. We still
    # consult the configured step table to validate `suggested_send_day`
    # on the first few emails — anything beyond the configured cadence
    # is reviewed without a hard expectation.
    step_specs = _active_step_specs()
    required_days = [int(spec["suggested_send_day"]) for spec in step_specs]
    previous_subject = ""
    for index, email in enumerate(emails):
        subject = str(email.get("subject", "") or "").strip()
        body = str(email.get("body_text", "") or "").strip()
        wc = len(body.split())
        if not subject:
            score -= 15
            issues.append(f"第 {index + 1} 封缺少主题。")
        if wc < 50:
            score -= 20
            issues.append(f"第 {index + 1} 封正文过短（{wc} 词）。")
            suggestions.append(f"请将第 {index + 1} 封扩展到至少 50 词。")
        elif wc > 260:
            score -= 10
            issues.append(f"第 {index + 1} 封正文过长（{wc} 词）。")
            suggestions.append(f"请精简第 {index + 1} 封以提升可读性。")
        if wc >= 50 and "\n\n" not in format_plaintext_email_body(body):
            score -= 8
            issues.append(f"第 {index + 1} 封纯文本排版过于密集。")
            suggestions.append(f"请在第 {index + 1} 封中加入明显的段落分隔与单独一行收尾。")

        # Only flag a send-day mismatch when the LLM actually emitted
        # a value. Missing/None is the common case (the model often
        # omits the field on 1-step sequences) and is handled by the
        # campaign-creation fallback, not by penalising the review.
        raw_day = email.get("suggested_send_day")
        if raw_day is not None and index < len(required_days):
            try:
                llm_day = int(raw_day)
            except (TypeError, ValueError):
                llm_day = None
            if llm_day is not None and llm_day != required_days[index]:
                score -= 5
                issues.append(f"第 {index + 1} 封的发送日应为 {required_days[index]}。")

        lowered_subject = subject.lower()
        if previous_subject and lowered_subject == previous_subject:
            score -= 8
            issues.append(f"第 {index + 1} 封的主题与上一封重复。")
        previous_subject = lowered_subject

    if not str(template_plan.get("cta_strategy", "") or "").strip():
        score -= 8
        issues.append("模板计划缺少 CTA 策略。")
    if not str(template_profile.get("tone", "") or "").strip():
        score -= 5
        issues.append("模板画像缺少语气指引。")

    company_name = str(lead.get("company_name", "") or "").strip()
    if company_name:
        personalization_hits = sum(
            1 for email in emails
            if company_name.lower() in str(email.get("body_text", "") or "").lower()
            or company_name.lower() in str(email.get("subject", "") or "").lower()
        )
        if personalization_hits == 0:
            score -= 10
            issues.append("整组序列未提及目标公司。")
            suggestions.append("请至少加入一条与该公司相关的切入点。")

    # Template adherence: when the template profile carries required
    # tokens (extracted from the user's historical emails), each
    # generated email should retain at least a configurable fraction
    # of them. Drift below the threshold flags the sequence for
    # auto-fix and, if that fails, raw-template fallback.
    tokens = list(required_tokens or [])
    worst_ratio = 1.0
    worst_missing: list[str] = []
    if tokens and emails:
        for index, email in enumerate(emails[:3]):
            body = str(email.get("body_text", "") or "")
            ratio, missing = _email_token_match_ratio(body, tokens)
            if ratio < worst_ratio:
                worst_ratio = ratio
                worst_missing = missing
        if worst_ratio < min_token_match_ratio:
            # Heavy penalty — this is the user's voice, not the LLM's.
            penalty = int(round(40 * (min_token_match_ratio - worst_ratio) * 2))
            score -= max(penalty, 12)
            preview = "、".join(f"「{m}」" for m in worst_missing[:5])
            issues.append(
                f"序列正在偏离用户模板的语气"
                f"（仅保留 {int(round(worst_ratio * 100))}% 的必含词，缺失：{preview}）。"
            )
            suggestions.append(
                "请将邮件重新对齐到模板：保留必含短语，"
                "并使用用户历史邮件的开场/收尾语气。"
            )

    status = "approved"
    if score < min_score or len(issues) > max_blocking_issues:
        status = "needs_review"

    return {
        "status": status,
        "score": max(score, 0),
        "issues": issues,
        "suggestions": suggestions,
        "min_score_required": min_score,
        "max_blocking_issues": max_blocking_issues,
        "blocking_issue_count": len(issues),
        "locale": locale,
        "template_adherence": {
            "required_tokens": tokens,
            "min_token_match_ratio": min_token_match_ratio,
            "worst_ratio": worst_ratio,
            "worst_missing": worst_missing,
        } if tokens else None,
    }
def _build_email_tools(llm: LLMTool, locale: str) -> list[ToolDef]:
    """Build the ReAct tool definitions for email validation."""
    rules = _get_locale_rules(locale)
    lang_name = rules["language"]
    formality = rules["formality"]
    salutation = rules["salutation"]
    closing = rules["closing"]
    rule_checks = "\n".join(f"  - {c}" for c in rules["checks"])

    async def tool_validate_emails(emails_json: str = "") -> str:
        """Validate the drafted email sequence for language correctness, formality, salutation format,
        and cultural appropriateness. Returns a validation report with issues and suggestions.
        Call this after drafting and after each revision.

        Args:
            emails_json: JSON string of the emails array (list of email objects).
        """
        if not emails_json or not emails_json.strip():
            return json.dumps({"passed": False, "issues": ["未提供邮件内容"], "suggestions": []})

        from tools.llm_output import parse_json as _parse
        try:
            submitted = _parse(emails_json, context="validate_emails")
            if submitted is None:
                return json.dumps({"passed": False, "issues": ["无法解析 emails_json 为 JSON"], "suggestions": []})
            emails_list = submitted.get("emails", submitted) if isinstance(submitted, dict) else submitted
            if not isinstance(emails_list, list):
                emails_list = []
        except Exception as e:
            return json.dumps({"passed": False, "issues": [f"解析失败：{e}"], "suggestions": []})
        rule_result = _rule_validate_emails_payload(emails_list)
        issues = list(rule_result["issues"])
        suggestions = list(rule_result["suggestions"])

        # Language/cultural quality check via LLM
        sample = "\n---\n".join(
            f"Email {i+1} subject: {e.get('subject','')}\nBody: {e.get('body_text','')[:400]}"
            for i, e in enumerate(emails_list[:3])
        )
        validation_prompt = (
            f"You are a {lang_name} language expert and B2B communication specialist.\n"
            f"Locale: {locale} | Language: {lang_name} | Formality: {formality}\n"
            f"Expected salutation: {salutation}\n"
            f"Expected closing: {closing}\n\n"
            f"Validation rules:\n{rule_checks}\n\n"
            f"Emails to validate:\n{sample}\n\n"
            f"Check each email against ALL rules. Return JSON:\n"
            f'{{"passed": true/false, "language_correct": true/false, '
            f'"formality_correct": true/false, "salutation_correct": true/false, '
            f'"commercial_quality": true/false, "sequence_progression": true/false, '
            f'"issues": ["..."], "suggestions": ["..."]}}\n'
            f"Also verify: buyer relevance, concrete seller value, low-friction CTA, and that the 3 emails progress instead of repeating.\n"
            f"Be specific: mention which email number has which problem.\n"
            f"\n"
            f"CRITICAL OUTPUT LANGUAGE: All string values inside the JSON — especially the 'issues' and "
            f"'suggestions' arrays — MUST be written in Simplified Chinese (简体中文), regardless of the "
            f"language of the emails being validated. Reference each email as '第 N 封' (e.g. 第 1 封, 第 2 封, 第 3 封)."
        )
        try:
            raw = await llm.generate(
                validation_prompt,
                system=f"You are a strict {lang_name} language and B2B communication validator. Return only JSON.",
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            from tools.llm_output import parse_json as _parse2
            llm_result = _parse2(raw, context="validate_emails_llm")
            if llm_result and isinstance(llm_result, dict):
                issues.extend(_localize_validator_list(llm_result.get("issues", [])))
                suggestions.extend(_localize_validator_list(llm_result.get("suggestions", [])))
                if not llm_result.get("language_correct", True):
                    issues.append(f"语言未通过：邮件未完全使用 {lang_name}")
                if not llm_result.get("formality_correct", True):
                    issues.append(f"语气未通过：应为 {formality} 语气")
                if not llm_result.get("salutation_correct", True):
                    issues.append(f"称呼未通过：应为 '{salutation}'")
                if not llm_result.get("commercial_quality", True):
                    issues.append("商业质量未通过：序列过于泛化或对买方缺乏针对性")
                if not llm_result.get("sequence_progression", True):
                    issues.append("序列递进未通过：邮件之间缺乏清晰的递进关系")
        except Exception as e:
            logger.debug("[EmailCraft] LLM validation call failed: %s", e)

        return json.dumps({
            "passed": len(issues) == 0,
            "locale": locale,
            "language": lang_name,
            "issues": issues,
            "suggestions": suggestions,
            "expected_salutation": salutation,
            "expected_closing": closing,
        })

    step_count = _configured_sequence_steps()
    email_noun = "email" if step_count == 1 else "emails"
    return [
        ToolDef(
            name="validate_emails",
            description=(
                f"Validate the email sequence for {lang_name} language correctness, "
                f"formality ({formality}), salutation format, cultural appropriateness, "
                f"and structural requirements ({step_count} {email_noun}, correct types, 100-200 words each). "
                f"Returns pass/fail with specific issues and suggestions. "
                f"Call after drafting and after each revision."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "emails_json": {
                        "type": "string",
                        "description": "JSON string of the emails array to validate",
                    },
                },
                "required": ["emails_json"],
            },
            fn=tool_validate_emails,
        ),
    ]


async def _craft_for_lead(
    lead: dict,
    insight: dict,
    llm: LLMTool,
    semaphore: asyncio.Semaphore,
    *,
    email_template_examples: list[str] | None = None,
    email_template_notes: str = "",
    prepared_template_seed: dict[str, Any] | None = None,
    react_max_iterations: int = 3,
    hunt_id: str = "",
    hunt_round: int = 0,
) -> dict | None:
    """Generate an N-step email sequence for a single lead using a ReAct loop.

    The step count is resolved from ``Settings.email_sequence_steps`` via
    ``_active_step_specs()``; the default is 1 (single outreach email).

    Flow: Think → Draft → validate_emails → Revise (up to 2x) → Output JSON.

    Args:
        lead: Lead dict with company_name, website, industry, country_code, etc.
        insight: Company insight dict with company_name, products, etc.
        llm: LLMTool instance (shared, not closed here).
        semaphore: Concurrency limiter.
        react_max_iterations: Max ReAct iterations (default 3: draft + 2 revisions).
    """
    async with semaphore:
        # Hunter.io enrichment: when the lead has no decision_makers
        # (only role-based inboxes like info@/sales@), call Hunter to
        # find named contacts at the lead's domain. We *don't* persist
        # the discovered contacts to the lead dict — they live in the
        # returned ``hunter_contacts`` field for the UI / prompt and
        # for the per-lead waterfall pool.
        hunter_contacts: list[dict[str, Any]] = []
        if not lead.get("decision_makers"):
            hunter_contacts = await _enrich_lead_with_hunter(lead)
        effective_lead = lead
        if hunter_contacts:
            # Mutate a shallow copy so the in-memory state across
            # concurrent leads is not shared.
            effective_lead = dict(lead)
            effective_lead["decision_makers"] = [
                {
                    "name": " ".join(
                        part for part in (c.get("first_name"), c.get("last_name")) if part
                    ).strip() or c.get("email"),
                    "email": c.get("email"),
                    "title": c.get("position", ""),
                    "source": "hunter.io",
                }
                for c in hunter_contacts
            ]
            logger.info(
                "[EmailCraft] Hunter.io found %d contact(s) for %s (%s)",
                len(hunter_contacts), lead.get("company_name"), lead.get("website"),
            )
        target = choose_email_target(effective_lead)
        if not target.get("target_email"):
            logger.info("[EmailCraft] Skipping %s — no sendable email target", lead.get("company_name"))
            return None
        # Promote the enriched lead so downstream code (brief synthesis,
        # user_prompt, etc.) sees the Hunter-discovered decision makers
        # without renaming the variable everywhere.
        if hunter_contacts:
            lead = effective_lead
        settings = get_settings()
        default_locale = _get_locale(lead.get("country_code", ""))
        language_choice = await _select_email_language(
            lead,
            target,
            llm,
            default_locale=default_locale,
            language_mode=settings.email_language_mode,
            default_language=settings.email_default_language,
            fallback_language=settings.email_fallback_language,
        )
        locale = str(language_choice.get("chosen_locale", default_locale) or default_locale)
        company_name = insight.get("company_name", "Our Company")
        products = ", ".join(insight.get("products", []))
        rules = _get_locale_rules(locale)
        strategy_brief = await _synthesise_email_brief(lead, insight, target, llm)
        seed_profile = (prepared_template_seed or {}).get("template_profile")
        seed_plan = (prepared_template_seed or {}).get("template_plan")
        if isinstance(seed_profile, dict) and isinstance(seed_plan, dict):
            template_profile = copy.deepcopy(seed_profile)
            template_plan = copy.deepcopy(seed_plan)
        else:
            template_profile = await extract_template_profile(
                llm,
                examples=list(email_template_examples or []),
                lead=lead,
                insight=insight,
                notes=email_template_notes,
            )
            template_plan = await compose_template_plan(
                llm,
                lead=lead,
                insight=insight,
                template_profile=template_profile,
                notes=email_template_notes,
            )

        step_specs = _active_step_specs()
        objectives_block = "\n".join(
            f"Email {spec['sequence_number']}: {spec['objective']}" for spec in step_specs
        )
        sequence_noun = "email" if len(step_specs) == 1 else f"{len(step_specs)}-email sequence"
        signature = _sender_signature(settings)

        user_prompt = (
            f"## Your Company\n"
            f"Name: {company_name}\n"
            f"Products: {products}\n"
            f"Sender signature — every email MUST close with exactly this (no placeholders like [Your Name]):\n"
            f"{signature}\n\n"
            f"Summary: {insight.get('summary', '')}\n"
            f"Industries: {', '.join(insight.get('industries', []))}\n"
            f"Value propositions: {'; '.join(insight.get('value_propositions', []))}\n"
            f"Ideal customer profile: {insight.get('target_customer_profile', '')}\n\n"
            f"## Target Lead\n"
            f"Company: {lead.get('company_name', 'Unknown')}\n"
            f"Website: {lead.get('website', '')}\n"
            f"Industry: {lead.get('industry', 'Unknown')}\n"
            f"Description: {lead.get('description', '')}\n"
            f"Contact person: {lead.get('contact_person', 'Unknown')}\n"
            f"Known emails: {', '.join(lead.get('emails', [])) or 'none'}\n"
            f"Target email: {target.get('target_email', '')}\n"
            f"Target contact name: {target.get('target_name', '')}\n"
            f"Target contact title: {target.get('target_title', '')}\n"
            f"Target type: {target.get('target_type', '')}\n\n"
            f"Fit score: {lead.get('fit_score', lead.get('match_score', ''))}\n"
            f"Contactability score: {lead.get('contactability_score', '')}\n\n"
            f"## Locale\n"
            f"Locale: {locale}\n"
            f"Language: {rules['language']}\n"
            f"Formality: {rules['formality']}\n"
            f"Expected salutation: {rules['salutation']}\n"
            f"Expected closing: {rules['closing']}\n"
            f"Language selection reason: {language_choice.get('reason', '')}\n"
            f"Fallback used: {language_choice.get('fallback_used', False)}\n\n"
            f"## Recipient Email Type\n"
            f"{_recipient_type_hint(target)}\n\n"
            f"## Hunter.io Discovered Contacts (for this lead)\n"
            f"{_format_hunter_contacts(hunter_contacts)}\n\n"
            f"## Strategy Brief\n"
            f"Recipient profile: {strategy_brief.get('recipient_profile', '')}\n"
            f"Why this company may fit: {json.dumps(strategy_brief.get('why_this_company_may_fit', []), ensure_ascii=False)}\n"
            f"Best value angles: {json.dumps(strategy_brief.get('best_value_angles', []), ensure_ascii=False)}\n"
            f"Product focus: {json.dumps(strategy_brief.get('product_focus', []), ensure_ascii=False)}\n"
            f"Proof points to use: {json.dumps(strategy_brief.get('proof_points_to_use', []), ensure_ascii=False)}\n"
            f"Claims to avoid: {json.dumps(strategy_brief.get('claims_to_avoid', []), ensure_ascii=False)}\n"
            f"CTA strategy: {strategy_brief.get('cta_strategy', '')}\n"
            f"Tone guidance: {strategy_brief.get('tone_guidance', '')}\n"
            f"Personalization hooks: {json.dumps(strategy_brief.get('personalization_hooks', []), ensure_ascii=False)}\n\n"
            f"## Sequence Objectives\n"
            f"{objectives_block}\n\n"
            f"## 营销要求（每封邮件必须满足）\n"
            f"1. 每封邮件必须 hook 我司产品的至少 1 个具体卖点。优先从以下抽取：\n"
            f"   - 具体型号 / 规格（例：SX-5000 inverter at 98.5% efficiency）\n"
            f"   - 具体认证 / 标准（例：CE / RoHS / ISO 9001）\n"
            f"   - 具体客户 / 行业案例（例：与 X 公司的合作经验）\n"
            f"   - 具体差异化数字（例：交付周期 / 寿命 / 起订量）\n"
            f"2. 禁止使用泛化营销话术：「行业领先 / 高质量 / 全球服务 / 极具竞争力」等没有\n"
            f"   数据支撑的形容词。如需使用必须配上具体数字或案例。\n"
            f"3. 客户业务必须落到具体细节：引用「## Target Lead」中的某个具体产品线、\n"
            f"   市场或网站细节。避免「Your esteemed company」这种空话。\n"
            f"4. 每封邮件至少 1 个差异化角度（vs 行业通用方案）。\n"
            f"5. marketing angle 必须从「## Strategy Brief」的 best_value_angles /\n"
            f"   proof_points_to_use 抽取，不要重新发明卖点。\n\n"
            f"## Style Examples\n"
            f"{EMAIL_FEWSHOT_EXAMPLES}\n\n"
            f"## Template Guidance\n"
            f"Template source: {template_profile.get('source', 'auto_generated')}\n"
            f"Template notes: {email_template_notes}\n"
            f"Template profile: {json.dumps(template_profile, ensure_ascii=False)}\n"
            f"Template plan: {json.dumps(template_plan, ensure_ascii=False)}\n\n"
            f"Write the {sequence_noun} in {rules['language']}. "
            f"Preserve the user's historical style when examples are present, "
            f"but adapt the content to this buyer and the template plan. "
            f"Call validate_emails after drafting, then revise if needed."
        )

        tools = _build_email_tools(llm, locale)

        try:
            raw = await react_loop(
                system=_build_react_system(len(step_specs)),
                user_prompt=user_prompt,
                tools=tools,
                settings=None,
                max_iterations=react_max_iterations,
                required_json_fields=["locale", "emails"],
                model_scope="email_reasoning",
                hunt_id=hunt_id,
                agent="email_craft",
                hunt_round=hunt_round,
            )
        except Exception as e:
            logger.warning("[EmailCraft] ReAct loop failed for %s: %s", lead.get("company_name"), e)
            return None

        from tools.llm_output import EMAIL_SEQUENCE_DEFAULTS, EMAIL_SEQUENCE_REQUIRED, validate_dict
        parsed = parse_json(raw, context="EmailCraftAgent")
        if parsed is None:
            logger.warning("[EmailCraft] Unparseable output for %s", lead.get("company_name"))
            return None

        revised, validation_summary = await _validate_and_revise_sequence(
            llm,
            locale=locale,
            rules=rules,
            user_prompt=user_prompt,
            parsed_sequence=parsed,
            max_revisions=max(0, int(getattr(settings, "email_validation_max_revisions", 2) or 2)),
        )
        if revised is None:
            logger.warning("[EmailCraft] Validation/revision produced no usable emails for %s", lead.get("company_name"))
            return None

        validated = validate_dict(revised, EMAIL_SEQUENCE_REQUIRED, defaults=EMAIL_SEQUENCE_DEFAULTS, context="EmailCraftAgent")
        if validated is None or not validated.get("emails"):
            logger.warning("[EmailCraft] No emails in output for %s", lead.get("company_name"))
            return None

        emails = format_email_sequence_bodies(
            validated["emails"], locale=locale, signature=signature
        )
        # Resolve template-adherence expectations up-front so we pass
        # the same set to the initial review and to any auto-improve
        # round. This is the user's voice — if the LLM drifts, we
        # detect and either re-anchor or fall back to the raw template.
        required_tokens = _required_tokens_for_template(template_profile, settings)
        min_token_match_ratio = float(
            getattr(settings, "email_template_min_token_match_ratio", 0.5) or 0.5
        )
        review_summary = _review_email_sequence(
            lead,
            locale=locale,
            emails=emails,
            template_profile=template_profile,
            template_plan=template_plan,
            min_score=int(settings.email_review_min_score or 75),
            max_blocking_issues=int(settings.email_review_max_blocking_issues or 0),
            required_tokens=required_tokens,
            min_token_match_ratio=min_token_match_ratio,
        )
        optimized_sequence = {"locale": validated.get("locale", locale), "emails": emails}
        optimized_sequence, review_summary, review_optimization = await _auto_improve_reviewed_sequence(
            llm,
            locale=locale,
            rules=rules,
            user_prompt=user_prompt,
            current_sequence=optimized_sequence,
            lead=lead,
            template_profile=template_profile,
            template_plan=template_plan,
            min_score=int(settings.email_review_min_score or 75),
            max_blocking_issues=int(settings.email_review_max_blocking_issues or 0),
            validation_max_revisions=max(0, int(getattr(settings, "email_validation_max_revisions", 2) or 2)),
            max_rounds=max(0, int(getattr(settings, "email_review_auto_fix_rounds", 2) or 2)),
            required_tokens=required_tokens,
            min_token_match_ratio=min_token_match_ratio,
        )
        emails = format_email_sequence_bodies(
            list(optimized_sequence.get("emails", []) or emails),
            locale=locale,
            signature=signature,
        )

        # Final template-adherence gate: if the LLM output still
        # doesn't carry enough of the user's required phrases after
        # auto-improve, fall back to the raw template. The user's
        # voice wins over a fluent but off-brand LLM rewrite. Can
        # be disabled via ``email_template_fallback_enabled``.
        template_fallback_used = False
        if (
            bool(getattr(settings, "email_template_fallback_enabled", True))
            and required_tokens
            and email_template_examples
        ):
            worst_ratio = 1.0
            for email in emails:
                body = str(email.get("body_text", "") or "")
                ratio, _ = _email_token_match_ratio(body, required_tokens)
                if ratio < worst_ratio:
                    worst_ratio = ratio
            if worst_ratio < min_token_match_ratio:
                fallback_emails = _build_raw_template_fallback(
                    list(email_template_examples),
                    lead=lead,
                    target=target,
                    step_specs=step_specs,
                    locale=locale,
                )
                if fallback_emails:
                    logger.info(
                        "[EmailCraft] %s → template fallback (%.0f%% < %.0f%% threshold)",
                        lead.get("company_name"),
                        worst_ratio * 100,
                        min_token_match_ratio * 100,
                    )
                    emails = format_email_sequence_bodies(
                        fallback_emails, locale=locale, signature=signature
                    )
                    template_fallback_used = True
                    # Re-score the fallback so the rest of the pipeline
                    # (auto_send_eligible, review_status) sees a clean
                    # state.
                    review_summary = _review_email_sequence(
                        lead,
                        locale=locale,
                        emails=emails,
                        template_profile=template_profile,
                        template_plan=template_plan,
                        min_score=int(settings.email_review_min_score or 75),
                        max_blocking_issues=int(settings.email_review_max_blocking_issues or 0),
                        required_tokens=required_tokens,
                        min_token_match_ratio=min_token_match_ratio,
                    )

        logger.info("[EmailCraft] %s → %d emails in %s", lead.get("company_name"), len(emails), locale)

        return {
            "lead": lead,
            "locale": locale,
            "target": target,
            "language_choice": language_choice,
            "strategy_brief": strategy_brief,
            "validation_summary": validation_summary,
            "review_status": review_summary.get("status", validation_summary.get("status", "approved")),
            "emails": emails,
            "template_profile": template_profile,
            "template_plan": template_plan,
            "template_seed_source": str((prepared_template_seed or {}).get("source", "") or ""),
            "review_summary": review_summary,
            "review_optimization": review_optimization,
            "auto_send_eligible": _review_allows_send(review_summary, settings),
            "hunter_contacts": hunter_contacts,
        }


async def email_craft_node(state: HuntState) -> dict:
    """LangGraph node: concurrently generate email sequences for all leads.

    Each lead runs a ReAct loop (Think → Draft → Validate → Revise, max 3 iterations).
    Uses asyncio.Semaphore(email_gen_concurrency) to limit parallel LLM calls.

    Returns:
        Dict with 'email_sequences' list.
    """
    settings = get_settings()
    leads = state.get("leads", [])
    insight = state.get("insight")
    insight = insight if isinstance(insight, dict) else {}

    logger.info("[EmailCraftAgent] Starting — %d leads, ReAct max_iterations=%d",
                len(leads), settings.react_max_iterations)

    if not leads:
        logger.info("[EmailCraftAgent] No leads, skipping email generation")
        return {"email_sequences": [], "current_stage": "email_craft"}

    semaphore = asyncio.Semaphore(settings.email_gen_concurrency)
    hunt_id = state.get("hunt_id", "")
    hunt_round = state.get("hunt_round", 0)
    email_template_examples = list(state.get("email_template_examples", []) or [])
    email_template_notes = str(state.get("email_template_notes", "") or "")
    prepared_template_seed = state.get("template_seed") if isinstance(state.get("template_seed"), dict) else None
    llm = LLMTool(
        model_type="email",
        hunt_id=hunt_id,
        agent="email_craft",
        hunt_round=hunt_round,
    )

    # ── Per-lead personalization mode ─────────────────────────────────────
    # Every lead gets its own fully independent ReAct draft (no template
    # reuse), so each email is written for that specific company instead
    # of a group template with name substitution.
    if _personalize_per_lead_enabled(settings):
        craft_items: list[tuple[dict[str, Any], dict[str, str], list[dict[str, str]]]] = []
        for lead in leads:
            target = choose_email_target(lead)
            if not target.get("target_email"):
                logger.info("[EmailCraftAgent] Skipping %s — no sendable email target", lead.get("company_name"))
                continue
            craft_items.append((lead, target, expand_email_targets(lead)))

        async def _craft_personalized(item: tuple[dict[str, Any], dict[str, str], list[dict[str, str]]]) -> dict[str, Any] | None:
            lead, target, targets = item
            result = await _craft_for_lead(
                lead,
                insight,
                llm,
                semaphore,
                email_template_examples=email_template_examples,
                email_template_notes=email_template_notes,
                prepared_template_seed=prepared_template_seed,
                react_max_iterations=settings.react_max_iterations,
                hunt_id=hunt_id,
                hunt_round=hunt_round,
            )
            if result is None:
                return None
            result["targets"] = targets
            result["generation_mode"] = "personalized"
            return result

        try:
            results = await asyncio.gather(*(_craft_personalized(item) for item in craft_items))
        finally:
            await llm.close()

        email_sequences = [r for r in results if isinstance(r, dict)]
        logger.info(
            "[EmailCraftAgent] Completed (personalized per lead) — %d/%d email sequences generated",
            len(email_sequences), len(leads),
        )
        return {
            "email_sequences": email_sequences,
            "current_stage": "email_craft",
        }

    grouped_leads: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
    for lead in leads:
        target = choose_email_target(lead)
        if not target.get("target_email"):
            logger.info("[EmailCraftAgent] Skipping %s — no sendable email target", lead.get("company_name"))
            continue
        template_group = _derive_template_group(
            lead,
            target=target,
            locale=_get_locale(lead.get("country_code", "")),
        )
        grouped_leads.setdefault(template_group, []).append((lead, target, expand_email_targets(lead)))

    async def _generate_group_seed(group_key: str, seed_lead: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        result = await _craft_for_lead(
            seed_lead,
            insight,
            llm,
            semaphore,
            email_template_examples=email_template_examples,
            email_template_notes=email_template_notes,
            prepared_template_seed=prepared_template_seed,
            react_max_iterations=settings.react_max_iterations,
            hunt_id=hunt_id,
            hunt_round=hunt_round,
        )
        return group_key, result

    try:
        template_max_send_count = int(getattr(settings, "email_template_max_send_count", _DEFAULT_TEMPLATE_MAX_SEND_COUNT) or _DEFAULT_TEMPLATE_MAX_SEND_COUNT)
        seed_tasks = []
        batch_members: dict[str, list[tuple[dict[str, Any], dict[str, str], list[dict[str, str]]]]] = {}
        for group_key, members in grouped_leads.items():
            batch_size = max(1, template_max_send_count)
            for batch_index, start in enumerate(range(0, len(members), batch_size), start=1):
                version_group = _template_version_group(group_key, batch_index)
                current_batch = members[start:start + batch_size]
                batch_members[version_group] = current_batch
                seed_tasks.append(_generate_group_seed(version_group, current_batch[0][0]))
        seed_results = await asyncio.gather(*seed_tasks)

        template_results = {group_key: result for group_key, result in seed_results if result is not None}
        email_sequences: list[dict[str, Any]] = []
        for version_group, members in batch_members.items():
            template_result = template_results.get(version_group)
            if template_result is None:
                continue
            base_group = version_group.rsplit("|v", 1)[0]
            template_assigned_count = len(members)
            for index, (lead, target, targets) in enumerate(members, start=1):
                applied = _apply_template_to_lead(
                    template_result,
                    lead=lead,
                    target=target,
                    template_group=version_group,
                    template_index=index,
                    template_assigned_count=template_assigned_count,
                    template_max_send_count=template_max_send_count,
                )
                applied["template_group_base"] = base_group
                applied["targets"] = targets
                if index > 1:
                    personalized = await _personalize_template_sequence(
                        llm,
                        base_sequence=applied,
                        lead=lead,
                        target=target,
                        insight=insight,
                    )
                    if isinstance(personalized, dict):
                        validated = validate_dict(
                            personalized,
                            EMAIL_SEQUENCE_REQUIRED,
                            defaults=EMAIL_SEQUENCE_DEFAULTS,
                            context="EmailCraftTemplatePersonalizer",
                        )
                        if validated is not None and validated.get("emails"):
                            applied["emails"] = validated["emails"]
                            review_summary = _review_email_sequence(
                                lead,
                                locale=str(applied.get("locale", "en_US") or "en_US"),
                                emails=validated["emails"],
                                template_profile=applied.get("template_profile", {}) or {},
                                template_plan=applied.get("template_plan", {}) or {},
                                min_score=int(settings.email_review_min_score or 75),
                                max_blocking_issues=int(settings.email_review_max_blocking_issues or 0),
                            )
                            optimized_sequence, review_summary, review_optimization = await _auto_improve_reviewed_sequence(
                                llm,
                                locale=str(applied.get("locale", "en_US") or "en_US"),
                                rules=_get_locale_rules(str(applied.get("locale", "en_US") or "en_US")),
                                user_prompt=(
                                    f"Personalize this approved template sequence for {lead.get('company_name', '')} "
                                    f"and the chosen contact {target.get('target_name', '')} <{target.get('target_email', '')}>."
                                ),
                                current_sequence={
                                    "locale": str(applied.get("locale", "en_US") or "en_US"),
                                    "emails": validated["emails"],
                                },
                                lead=lead,
                                template_profile=applied.get("template_profile", {}) or {},
                                template_plan=applied.get("template_plan", {}) or {},
                                min_score=int(settings.email_review_min_score or 75),
                                max_blocking_issues=int(settings.email_review_max_blocking_issues or 0),
                                validation_max_revisions=max(0, int(getattr(settings, "email_validation_max_revisions", 2) or 2)),
                                max_rounds=max(0, int(getattr(settings, "email_review_auto_fix_rounds", 2) or 2)),
                            )
                            applied["emails"] = format_email_sequence_bodies(
                                list(optimized_sequence.get("emails", []) or validated["emails"]),
                                locale=str(applied.get("locale", "en_US") or "en_US"),
                                signature=str(applied.get("signature", "") or "") or None,
                            )
                            applied["review_summary"] = review_summary
                            applied["validation_summary"] = {
                                "passed": review_summary["status"] == "approved",
                                "status": review_summary["status"],
                                "issues": list(review_summary.get("issues", [])),
                                "suggestions": list(review_summary.get("suggestions", [])),
                            }
                            applied["review_status"] = review_summary["status"]
                            applied["review_optimization"] = review_optimization
                            applied["auto_send_eligible"] = _review_allows_send(review_summary, settings)
                            applied["generation_mode"] = "template_pool_personalized"
                email_sequences.append(applied)
    finally:
        await llm.close()

    logger.info("[EmailCraftAgent] Completed — %d/%d email sequences generated",
                len(email_sequences), len(leads))

    return {
        "email_sequences": email_sequences,
        "current_stage": "email_craft",
    }
