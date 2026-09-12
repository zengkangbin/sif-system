"""Keyword, persona and Listing analysis for the MVP."""

from __future__ import annotations

import json
import math
import os
import re
import asyncio
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit


TYPE_LABELS = ("核心词", "长尾词", "功能词", "场景词", "人群词")
SCENE_WORDS = {"home", "office", "travel", "camping", "outdoor", "kitchen", "bedroom", "car", "road", "gym", "work"}
AUDIENCE_WORDS = {"women", "men", "kids", "baby", "senior", "professional", "beginner", "student", "family", "mom", "dad"}
FEATURE_WORDS = {"portable", "wireless", "fast", "foldable", "waterproof", "rechargeable", "adjustable", "durable", "lightweight", "large", "small", "compatible", "non-slip"}


STRATEGIES: dict[str, dict[str, str]] = {
    "comprehensive": {
        "name": "综合增长",
        "persona": "你是一名 Amazon 综合增长策略负责人，兼具 SEO、语义搜索、消费者洞察、Listing 转化和广告协同能力。",
        "objective": "同时兼顾 Amazon SEO、语义搜索、Rufus/Alexa 购物问答、消费者可读性和 Listing 转化。",
        "instruction": "完整分析关键词、消费者需求、购买障碍、差异化卖点，并建立 Keyword → Intent → Benefit → Content Placement 映射。",
    },
    "seo_growth": {
        "name": "SEO Ranking Growth",
        "persona": "你是一名 Amazon SEO 排名增长专家，擅长从竞品流量词建立 Keyword Pool、判断搜索意图和自然排名机会，但不会为了覆盖而牺牲相关性和可读性。",
        "objective": "扩大高价值关键词的有效覆盖，提高 Amazon Search SEO、语义搜索自然召回和排名潜力。",
        "instruction": "重点建立 Keyword Pool，识别 P0-P4、竞品流量入口、关键词缺口、共同覆盖词、购买意愿词和低相关高流量陷阱词，并给出 SEO 与广告联动方案。",
    },
    "conversion_first": {
        "name": "Conversion First",
        "persona": "你是一名 Amazon 转化率优化专家，像消费者研究员和 CRO 文案顾问一样，优先解决购买疑虑并把产品事实转化为清晰的消费者利益。",
        "objective": "提高进入 Listing 后消费者的购买概率，而不是单纯增加关键词数量。",
        "instruction": "按 Consumer Problem → Desired Outcome → Product Feature → Consumer Benefit → Proof → Objection Handling 组织内容，优先解决购买顾虑并规划 7 张图片叙事。",
    },
    "new_product": {
        "name": "New Product Differentiation",
        "persona": "你是一名新品定位与差异化策略专家，擅长识别竞品同质化和未解决问题，把真实差异转化为可验证、可理解的购买理由。",
        "objective": "帮助新品、升级版或结构差异明显的产品找到可被消费者理解和验证的差异化购买理由。",
        "instruction": "分析竞品共同卖点、同质化功能、竞品未解决的问题、我方独有功能和消费者利益；区分已验证需求、合理推断和待验证假设，并设计广告验证计划。",
    },
    "listing_refresh": {
        "name": "Listing Refresh & Second Growth",
        "persona": "你是一名 Amazon 老 Listing 诊断与二次增长专家，先区分流量、CTR、CVR、价格、评价、竞争、广告和产品问题，再决定哪些内容保留、弱化或重写。",
        "objective": "为已有运营历史但流量、排名、CTR、CVR 或销量停滞/下降的 ASIN 找到真实增长限制。",
        "instruction": "区分流量、CTR、CVR、价格、评价、竞争、广告和产品本身问题，识别当前 Listing 的保留、删除/弱化、新增内容，并说明新旧版本变化理由。",
    },
}

GPT_PROVIDERS: dict[str, dict[str, str]] = {
    "official": {"name": "官网", "description": "直接调用 OpenAI 官网"},
    "middleman": {"name": "中转站", "description": "调用配置的 OpenAI 兼容中转接口"},
    "auto": {"name": "自动故障切换", "description": "中转站失败后切换官网"},
}


def normalize_gpt_provider(value: Any) -> str:
    key = str(value or "official").strip().lower()
    return key if key in GPT_PROVIDERS else "official"


def gpt_provider_info(value: Any) -> dict[str, str]:
    key = normalize_gpt_provider(value)
    return {"id": key, **GPT_PROVIDERS[key]}


def normalize_strategy(value: Any) -> str:
    key = str(value or "comprehensive").strip().lower()
    return key if key in STRATEGIES else "comprehensive"


def strategy_info(value: Any) -> dict[str, str]:
    key = normalize_strategy(value)
    return {"id": key, **STRATEGIES[key]}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 1.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _is_retryable_gpt_error(exc: Exception) -> bool:
    """Return whether a GPT request may succeed if sent again."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError)):
        return True

    error_name = type(exc).__name__.lower()
    if "timeout" in error_name or "connection" in error_name:
        return True

    status_code = getattr(exc, "status_code", None)
    return status_code in {408, 409, 429} or (
        isinstance(status_code, int) and status_code >= 500
    )


class _GPTFailoverError(RuntimeError):
    """A request failed on one or more configured GPT endpoints.

    The individual exceptions are retained for diagnostics, while the string
    representation only contains endpoint names and exception text (never API
    keys).
    """

    def __init__(self, failures: list[tuple[dict[str, str], Exception]]) -> None:
        self.failures = failures
        details = "; ".join(
            f"{endpoint['name']}({endpoint['label']}): {type(error).__name__}: {error}"
            for endpoint, error in failures
        )
        super().__init__(details or "GPT请求失败")


def _gpt_endpoints(
    provider: str = "official",
    model: str = "gpt-5.6-sol",
) -> list[dict[str, str]]:
    """Return configured endpoints in the operator-selected order."""

    provider = normalize_gpt_provider(provider)
    selected_model = str(model or "gpt-5.6-sol").strip() or "gpt-5.6-sol"
    middleman: dict[str, str] | None = None
    primary_key = os.getenv("OPENAI_API_KEY", "").strip()
    primary_base = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
    if primary_key:
        middleman = {
            "name": "中转站",
            "id": "middleman",
            "api_key": primary_key,
            "base_url": primary_base,
            "model": selected_model,
            "label": urlsplit(primary_base).netloc or "中转站默认节点",
        }

    official: dict[str, str] | None = None
    official_key = os.getenv("OPENAI_OFFICIAL_API_KEY", "").strip()
    official_base = (
        os.getenv("OPENAI_OFFICIAL_BASE_URL", "https://api.openai.com/v1")
        .strip()
        .rstrip("/")
    )
    if official_key:
        official = {
            "name": "官网",
            "id": "official",
            "api_key": official_key,
            "base_url": official_base,
            "model": selected_model,
            "label": urlsplit(official_base).netloc or "api.openai.com",
        }

    if provider == "middleman":
        return [middleman] if middleman else []
    if provider == "auto":
        return [endpoint for endpoint in (middleman, official) if endpoint]
    return [official] if official else []


def _openai_client_kwargs(endpoint: dict[str, str], timeout: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "api_key": endpoint["api_key"],
        "timeout": timeout,
        # Retries are handled explicitly by _chat_json_with_retry so the
        # failover decision happens after the configured middleman attempts.
        "max_retries": 0,
    }
    if endpoint.get("base_url"):
        kwargs["base_url"] = endpoint["base_url"]
    return kwargs


async def _close_openai_client(client: Any) -> None:
    """Close an AsyncOpenAI client when the installed SDK exposes close()."""

    close = getattr(client, "close", None)
    if not close:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        # A close failure must not hide the useful request result/error.
        return


async def _chat_json_with_failover(
    *,
    messages: list[dict[str, str]],
    provider: str = "official",
    model: str = "gpt-5.6-sol",
) -> tuple[Any, dict[str, str]]:
    """Call the operator-selected GPT endpoint sequence."""

    provider = normalize_gpt_provider(provider)
    endpoints = _gpt_endpoints(provider, model)
    if not endpoints:
        raise RuntimeError(f"所选GPT节点【{gpt_provider_info(provider)['name']}】未配置API Key")

    from openai import AsyncOpenAI

    timeout = _env_float("OPENAI_TIMEOUT_SECONDS", 120.0)
    retries = _env_int("OPENAI_MAX_RETRIES", 1)
    delay_seconds = _env_float("OPENAI_RETRY_DELAY_SECONDS", 2.0, minimum=0.0)
    failures: list[tuple[dict[str, str], Exception]] = []

    for index, endpoint in enumerate(endpoints):
        client: Any | None = None
        try:
            client = AsyncOpenAI(**_openai_client_kwargs(endpoint, timeout))
            response = await _chat_json_with_retry(
                client,
                model=endpoint["model"],
                messages=messages,
                retries=retries,
                delay_seconds=delay_seconds,
            )
            return response, endpoint
        except Exception as exc:
            failures.append((endpoint, exc))
            # The official endpoint is a fallback for transient transport or
            # server failures. Invalid requests (for example a bad model or
            # malformed payload) should be surfaced directly instead of
            # duplicating the same invalid request against another endpoint.
            if provider == "auto" and index == 0 and len(endpoints) > 1 and _is_retryable_gpt_error(exc):
                continue
            raise _GPTFailoverError(failures) from exc
        finally:
            if client is not None:
                await _close_openai_client(client)

    raise _GPTFailoverError(failures)


async def _chat_json_with_retry(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, str]],
    retries: int,
    delay_seconds: float,
) -> Any:
    """Call GPT with explicit, bounded retries for transient failures."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except Exception as exc:
            last_error = exc
            if not _is_retryable_gpt_error(exc) or attempt >= retries:
                raise
            await asyncio.sleep(delay_seconds * (2**attempt))

    # The loop always returns or raises, but retaining the guard keeps static
    # type checkers aware that this helper cannot return None accidentally.
    raise last_error or RuntimeError("GPT请求失败")


def _number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else 0.0


def _ratio(value: Any) -> float:
    """Read either a 0-1 ratio or a percentage string such as ``"82%"``."""
    raw = str(value).strip() if value is not None else ""
    number = _number(value)
    if "%" in raw or number > 1:
        return number / 100
    return number


def _row_value(row: Any, *keys: str) -> Any:
    if isinstance(row, dict):
        for key in keys:
            if row.get(key) is not None:
                return row[key]
    elif isinstance(row, str) and "keyword" in keys:
        return row
    return None


def _keyword_type(term: str, product_text: str) -> str:
    words = set(re.findall(r"[a-z0-9-]+", term.lower()))
    if words & SCENE_WORDS:
        return "场景词"
    if words & AUDIENCE_WORDS:
        return "人群词"
    if words & FEATURE_WORDS or any(word in product_text for word in words if len(word) > 4):
        return "功能词"
    if len(words) >= 3:
        return "长尾词"
    return "核心词"


def _priority(score: float) -> str:
    if score >= 0.72:
        return "P0"
    if score >= 0.52:
        return "P1"
    if score >= 0.34:
        return "P2"
    if score >= 0.18:
        return "P3"
    return "P4"


def build_keywords(product: dict[str, Any], sif_data: dict[str, Any]) -> list[dict[str, Any]]:
    # Include the free-form operator notes when judging keyword relevance and
    # type. Legacy fields are still read for old saved jobs.
    product_text = " ".join(
        str(product.get(key, ""))
        for key in (
            "product_name",
            "brand",
            "category",
            "material",
            "specifications",
            "functions",
            "packaging",
            "selling_points",
            "target_customer",
            "claims_allowed",
            "claims_forbidden",
            "operator_notes",
        )
    ).lower()
    merged: dict[str, dict[str, Any]] = {}
    for item in sif_data.get("items", []):
        asin = item.get("asin", "")
        rows: list[dict[str, Any]] = []
        signals = item.get("signals") or {}
        footprint = item.get("footprint") or {}
        rows.extend(signals.get("top_keywords") or [])
        rows.extend(signals.get("secondary_signals", {}).get("keywords") or [])
        rows.extend(footprint.get("keywords") or [])
        for row in rows:
            term = str(_row_value(row, "keyword", "term") or "").strip().lower()
            if not term:
                continue
            current = merged.setdefault(term, {"term": term, "source_asins": set(), "search_volume": 0.0, "natural_ratio": 0.0, "mentions": 0})
            current["source_asins"].add(asin)
            current["search_volume"] = max(current["search_volume"], _number(_row_value(row, "search_volume", "searchVolume", "estSearchesNum")))
            current["natural_ratio"] = max(current["natural_ratio"], _ratio(_row_value(row, "natural_ratio", "naturalRatio")))
            current["mentions"] += 1

    max_volume = max((row["search_volume"] for row in merged.values()), default=1.0)
    max_mentions = max((row["mentions"] for row in merged.values()), default=1.0)
    result: list[dict[str, Any]] = []
    for row in merged.values():
        relevance = min(1.0, len(row["source_asins"]) / max(1, len(sif_data.get("items", []))))
        volume_score = math.log1p(row["search_volume"]) / math.log1p(max_volume) if max_volume > 1 else 0.0
        coverage = row["mentions"] / max_mentions
        score = 0.45 * volume_score + 0.30 * relevance + 0.15 * coverage + 0.10 * min(1.0, row["natural_ratio"])
        kind = _keyword_type(row["term"], product_text)
        result.append(
            {
                "term": row["term"],
                "type": kind,
                "priority": _priority(score),
                "score": round(score, 3),
                "search_volume": int(row["search_volume"]),
                "natural_ratio": round(row["natural_ratio"], 4),
                "source_asins": sorted(row["source_asins"]),
                "reason": f"覆盖 {len(row['source_asins'])} 个竞品，搜索量、竞品覆盖和提及次数综合评分为 {score:.2f}",
                "confidence": round(min(0.99, 0.55 + 0.1 * len(row["source_asins"]) + 0.2 * coverage), 2),
            }
        )
    result.sort(key=lambda row: (-row["score"], -row["search_volume"], row["term"]))
    return result[:300]


def compact_sif_data(sif_data: dict[str, Any], limit_per_asin: int = 40) -> dict[str, Any]:
    def without_traffic_share(rows: list[Any]) -> list[Any]:
        cleaned: list[Any] = []
        for row in rows[:limit_per_asin]:
            if not isinstance(row, dict):
                cleaned.append(row)
                continue
            cleaned.append({key: value for key, value in row.items() if key not in {"traffic_share", "trafficShare"}})
        return cleaned

    compact: dict[str, Any] = {"profiles": sif_data.get("profiles", {}), "items": []}
    for item in sif_data.get("items", []):
        signals = item.get("signals") or {}
        footprint = item.get("footprint") or {}
        compact["items"].append(
            {
                "asin": item.get("asin"),
                "signals": {"top_keywords": without_traffic_share(signals.get("top_keywords") or [])},
                "footprint": {"keywords": without_traffic_share(footprint.get("keywords") or [])},
            }
        )
    return compact


def _fallback_selling_points(
    product: dict[str, Any],
    sif_data: dict[str, Any],
    keywords: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build selectable selling-point directions without inventing facts."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    notes = str(product.get("operator_notes") or "").strip()
    if notes:
        candidates.append({
            "id": "operator-facts",
            "title": "突出已确认的产品优势",
            "description": re.sub(r"\s+", " ", notes)[:180],
            "evidence": ["运营补充信息"],
        })
        seen.add("operator-facts")
    type_labels = {
        "功能词": "核心功能与使用效果",
        "场景词": "重点使用场景",
        "人群词": "目标人群适配",
        "长尾词": "细分需求与差异化",
        "核心词": "核心品类需求",
    }
    for row in keywords:
        kind = str(row.get("type") or "核心词")
        key = kind
        if key in seen or row.get("priority") not in {"P0", "P1", "P2"}:
            continue
        term = str(row.get("term") or "").strip()
        if not term:
            continue
        candidates.append({
            "id": f"keyword-{len(candidates) + 1}",
            "title": type_labels.get(kind, "高价值搜索需求"),
            "description": f"围绕高相关关键词“{term}”组织卖点，强调与该搜索需求直接相关且已确认的产品能力。",
            "evidence": [term, *list(row.get("source_asins") or [])[:3]],
        })
        seen.add(key)
        if len(candidates) >= 6:
            break
    if not candidates:
        candidates.append({
            "id": "verified-benefit",
            "title": "真实功能与适用场景",
            "description": "围绕已确认的功能、参数和使用场景组织内容，避免扩展未经验证的承诺。",
            "evidence": [str(product.get("product_name") or "产品资料")],
        })
    return candidates[:6]


async def propose_selling_points(
    product: dict[str, Any],
    sif_data: dict[str, Any],
    *,
    strategy: str = "comprehensive",
    gpt_provider: str = "official",
    gpt_model: str = "gpt-5.6-sol",
) -> dict[str, Any]:
    """Generate evidence-backed selling-point directions for operator choice."""
    strategy = normalize_strategy(strategy)
    gpt_provider = normalize_gpt_provider(gpt_provider)
    selected_strategy = strategy_info(strategy)
    keywords = build_keywords(product, sif_data)
    fallback = _fallback_selling_points(product, sif_data, keywords)
    if not _gpt_endpoints(gpt_provider, gpt_model):
        return {"keywords": keywords, "selling_point_candidates": fallback, "meta": {"mode": "local-fallback"}}
    schema = {
        "selling_point_candidates": [{
            "id": "point-1",
            "title": "中文卖点名称",
            "description": "中文说明，说明消费者利益和内容方向",
            "evidence": ["关键词、竞品ASIN或运营事实"],
        }]
    }
    prompt = (
        f"你是亚马逊商品策略分析师，采用【{selected_strategy['name']}】视角。"
        "根据产品资料、SIF竞品数据和关键词，提出5-8个可供运营人员选择的商品卖点方向。"
        "卖点必须使用中文，每个卖点说明消费者利益，并列出实际依据。不得编造产品参数、认证、功效或竞品数据。"
        "卖点之间要有明显区别，避免只是关键词改写。只返回严格JSON，不要Markdown。\n"
        f"产品资料：{json.dumps(product, ensure_ascii=False)}\n"
        f"SIF数据：{json.dumps(compact_sif_data(sif_data), ensure_ascii=False)}\n"
        f"关键词：{json.dumps(keywords[:60], ensure_ascii=False)}\n"
        f"输出结构：{json.dumps(schema, ensure_ascii=False)}"
    )
    try:
        response, endpoint = await _chat_json_with_failover(
            messages=[
                {"role": "system", "content": "你只输出可被程序解析的JSON。"},
                {"role": "user", "content": prompt},
            ],
            provider=gpt_provider,
            model=gpt_model,
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        candidates = []
        for index, item in enumerate(payload.get("selling_point_candidates") or []):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            description = str(item.get("description") or "").strip()
            if not title or not description:
                continue
            candidates.append({
                "id": str(item.get("id") or f"point-{index + 1}").strip(),
                "title": title,
                "description": description,
                "evidence": [str(value).strip() for value in (item.get("evidence") or []) if str(value).strip()][:8],
            })
        if not candidates:
            candidates = fallback
        return {
            "keywords": keywords,
            "selling_point_candidates": candidates[:8],
            "meta": {"mode": f"openai-{endpoint['id']}", "model": endpoint["model"], "strategy": strategy},
        }
    except Exception as exc:
        return {
            "keywords": keywords,
            "selling_point_candidates": fallback,
            "risks": [f"候选卖点GPT生成失败，已使用本地候选：{type(exc).__name__}"],
            "meta": {"mode": "local-fallback", "strategy": strategy},
        }


def limit_title(value: Any, limit: int = 75) -> str:
    """Keep an Amazon title within the requested character limit."""
    return str(value or "").strip()[:limit]


def clean_review_text(value: Any) -> str:
    """Remove legacy disclaimer prefixes from simulated buyer comments.

    Older results may contain a model-added ``[Internal simulated buyer
    feedback ...]`` prefix. The current prompt no longer asks for it, and
    stripping it here keeps regenerated and historical displays consistent.
    """

    text = str(value or "").strip()
    return re.sub(
        r"^\s*\[(?:internal\s+simulated\s+buyer\s+feedback|内部模拟买家(?:反馈|评论))[^\]]*\]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()


def normalize_review_entry(value: Any) -> dict[str, str]:
    """Normalize current and legacy simulated-review shapes.

    New GPT responses use title/content pairs in English and Chinese. Older
    saved jobs contain plain strings, so those are retained as the content
    with a neutral fallback title.
    """

    if isinstance(value, dict):
        title = str(
            value.get("title")
            or value.get("review_title")
            or value.get("headline")
            or ""
        ).strip()
        title_zh = str(
            value.get("title_zh")
            or value.get("review_title_zh")
            or value.get("headline_zh")
            or ""
        ).strip()
        content = clean_review_text(
            value.get("content")
            or value.get("review")
            or value.get("review_content")
            or value.get("comment")
            or value.get("text")
            or value.get("body")
            or ""
        )
        content_zh = clean_review_text(
            value.get("content_zh")
            or value.get("review_zh")
            or value.get("review_content_zh")
            or value.get("comment_zh")
            or value.get("text_zh")
            or value.get("body_zh")
            or ""
        )
    else:
        title = "买家使用体验"
        title_zh = "买家使用体验"
        content = clean_review_text(value)
        content_zh = ""
    return {
        "title": title or "Buyer Experience",
        "title_zh": title_zh or "买家使用体验",
        "content": content,
        "content_zh": content_zh,
    }


def _section_from_result(
    result: dict[str, Any],
    section: str,
    qa_index: int | None = None,
    qa_count: int | None = None,
    review_count: int | None = None,
) -> Any:
    listing = result.get("listing") or {}
    if section == "title":
        return {"title": limit_title(listing.get("title")), "title_zh": str(listing.get("title_zh") or "").strip()}
    if section == "bullets":
        return {"bullets": list(listing.get("bullets") or [])[:5], "bullets_zh": list(listing.get("bullets_zh") or [])[:5]}
    if section == "qa":
        qa = list(listing.get("qa") or [])
        if qa_index is None:
            return qa[:qa_count] if qa_count else qa
        if qa_index < 0 or qa_index >= len(qa):
            raise ValueError("Q&A索引无效")
        return qa[qa_index]
    if section == "reviews":
        entries = [normalize_review_entry(item) for item in (listing.get("internal_review_drafts") or [])]
        entries = [item for item in entries if item["content"] or item["content_zh"]]
        return entries[:review_count] if review_count else entries
    raise ValueError("不支持的内容类型")


def _local_result(product: dict[str, Any], sif_data: dict[str, Any], strategy: str = "comprehensive") -> dict[str, Any]:
    strategy = normalize_strategy(strategy)
    selected_strategy = strategy_info(strategy)
    keywords = build_keywords(product, sif_data)
    top_terms = [row["term"] for row in keywords if row["priority"] in {"P0", "P1"}][:5]
    name = product.get("product_name") or "产品"
    operator_notes = str(product.get("operator_notes") or "").strip()
    notes_excerpt = re.sub(r"\s+", " ", operator_notes)[:160]
    material = product.get("material") or ""
    functions = product.get("functions") or ""
    title_parts = [name, material, *top_terms[:3]]
    title = limit_title(" ".join(part.strip() for part in title_parts if part.strip()))
    operator_context = (
        f"运营补充信息（发布前核验）：{notes_excerpt}"
        if notes_excerpt
        else "围绕已确认的产品功能组织卖点，避免使用未经证实的承诺。"
    )
    bullets = [
        f"核心功能：{functions or operator_context}",
        f"材质与结构：{material or ('请从运营补充信息中核对材质、结构和参数后再发布。' if notes_excerpt else '请补充材质和结构信息后重新生成。')}",
        "使用场景：结合高相关场景词，说明产品如何帮助目标用户完成具体任务。",
        "差异化卖点：优先使用多个竞品共同验证、且产品事实库能够支持的卖点。",
        "包装与限制：根据包装清单、兼容范围和使用限制进行准确说明。",
    ]
    personas = [
        {"name": "核心功能需求用户", "pain_points": ["希望快速判断产品是否解决当前问题"], "purchase_motivations": ["功能清晰", "关键词与需求匹配"], "usage_scenarios": ["日常使用", "目标场景使用"], "purchase_concerns": ["参数是否真实", "是否适配"], "evidence": top_terms[:3]},
        {"name": "场景型用户", "pain_points": ["不确定产品在特定场景中的表现"], "purchase_motivations": ["便携或易用", "场景适配"], "usage_scenarios": ["家庭", "办公室", "出行"], "purchase_concerns": ["尺寸", "包装内容"], "evidence": [row["term"] for row in keywords if row["type"] == "场景词"][:3]},
    ]
    result = {
        "strategy": selected_strategy,
        "keywords": keywords,
        "consumer_profiles": personas,
        "content_strategy": {"positioning": f"{selected_strategy['objective']} 围绕 {name} 的真实产品事实组织内容。", "selling_point_order": ["核心功能", "材质结构", "使用场景", "差异化", "包装与限制"], "search_intent_map": top_terms},
        "listing": {
            "title": title,
            "title_zh": title,
            "bullets": bullets,
            "bullets_zh": bullets,
            "qa": [
                {"question": "What usage scenarios is this product suitable for?", "answer": "Use it according to the confirmed scenarios and product specifications without extending unverified claims.", "question_zh": "这款产品适合什么使用场景？", "answer_zh": "请根据已确认的场景和产品参数使用，不扩展未验证的使用承诺。"},
                {"question": "What is included in the package?", "answer": "Please refer to the confirmed package contents before purchase.", "question_zh": "包装中包含哪些内容？", "answer_zh": product.get("packaging") or (f"请以运营补充信息中的包装清单为准：{notes_excerpt}" if notes_excerpt else "请补充包装清单。")},
                {"question": "Are there any usage limitations?", "answer": "Please follow the confirmed compatibility range and limitations in the product facts.", "question_zh": "产品使用时有什么限制？", "answer_zh": "请以产品事实库中的兼容范围和限制条件为准。"},
            ],
            "internal_review_drafts": [
                {"title": "Clear Fit and Compatibility", "title_zh": "适配范围清晰", "content": "After buying it, I first checked whether it matched my device. The main function and supported use cases were clearly explained.", "content_zh": "买到后我先确认它是否适配自己的设备，产品信息对主要功能和适用范围说明得比较清楚。"},
                {"title": "Practical for Everyday Use", "title_zh": "日常使用实用", "content": "In everyday use, I pay attention to the feel, size, and small details. Showing these points clearly makes the product easier to evaluate.", "content_zh": "日常使用时我比较在意手感、尺寸和细节做工，把这些卖点说明白后会更容易判断。"},
                {"title": "Worth Checking Before Ordering", "title_zh": "下单前需要确认", "content": "I also checked the package contents and compatibility range after using it, so I knew the accessories and usage conditions were right for me.", "content_zh": "使用后我还会核对包装清单和兼容范围，确认配件齐全、使用条件适合自己。"},
                {"title": "Simple to Evaluate", "title_zh": "信息容易判断", "content": "The product details helped me compare the main function, intended use, and setup requirements before deciding whether it fit my needs.", "content_zh": "产品信息帮助我在下单前比较主要功能、适用场景和使用要求，更容易判断是否适合自己。"},
                {"title": "Check the Details for Your Setup", "title_zh": "需要结合自身情况确认", "content": "I focused on the available dimensions, compatibility details, and package information so I could make a more informed choice for my setup.", "content_zh": "我重点核对了尺寸、兼容信息和包装内容，再结合自己的使用环境做决定。"},
            ],
        },
        "shopping_intents": [
            {"intent": "快速找到适合目标场景的产品", "sample_queries": [f"What is a good {name} for everyday use?"], "evidence": top_terms[:3]},
            {"intent": "确认功能、兼容性和包装", "sample_queries": ["What does it include?", "Is it compatible with my setup?"], "evidence": ["产品参数", "包装清单", "兼容性"]},
        ],
        "risks": ["演示模式结果不是实时SIF数据。", "内部模拟直评仅用于内容研究，不能冒充真实消费者评价发布。"],
        "meta": {"mode": "local-demo", "keyword_count": len(keywords)},
    }
    result["meta"]["strategy"] = strategy
    return result


async def analyze(
    product: dict[str, Any],
    sif_data: dict[str, Any],
    regeneration_note: str = "",
    strategy: str = "comprehensive",
    gpt_provider: str = "official",
    gpt_model: str = "gpt-5.6-sol",
    selected_selling_points: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use GPT when configured, with a deterministic local fallback."""
    strategy = normalize_strategy(strategy)
    gpt_provider = normalize_gpt_provider(gpt_provider)
    gpt_model = str(gpt_model or "gpt-5.6-sol").strip() or "gpt-5.6-sol"
    selected_points = list(selected_selling_points or [])
    product_for_fallback = dict(product)
    if selected_points:
        selected_text = "；".join(
            f"{item.get('title', '')}：{item.get('description', '')}"
            for item in selected_points
            if isinstance(item, dict)
        )
        product_for_fallback["selling_points"] = selected_text
        product_for_fallback["functions"] = selected_text
    selected_strategy = strategy_info(strategy)
    if not _gpt_endpoints(gpt_provider, gpt_model):
        fallback = _local_result(product_for_fallback, sif_data, strategy)
        fallback["selected_selling_points"] = selected_points
        fallback.setdefault("risks", []).insert(0, f"所选GPT节点【{gpt_provider_info(gpt_provider)['name']}】未配置，已使用本地兜底分析。")
        fallback["meta"].update({"mode": "local-fallback", "gpt_provider": gpt_provider, "model": gpt_model})
        return fallback

    try:
        schema_hint = {
            "strategy": {"id": strategy, "name": selected_strategy["name"]},
            "keywords": [{"term": "", "type": "核心词|长尾词|功能词|场景词|人群词", "priority": "P0|P1|P2|P3|P4", "reason": "", "source_asins": [], "search_volume": 0, "confidence": 0.0}],
            "consumer_profiles": [{"name": "（中文画像名称）", "pain_points": ["（中文痛点）"], "purchase_motivations": ["（中文购买动机）"], "usage_scenarios": ["（中文使用场景）"], "purchase_concerns": ["（中文购买顾虑）"], "evidence": ["（中文证据说明）"]}],
            "content_strategy": {"positioning": "", "selling_point_order": [], "search_intent_map": []},
            "listing": {"title": "English title", "title_zh": "中文标题翻译", "bullets": ["English bullet 1", "English bullet 2", "English bullet 3", "English bullet 4", "English bullet 5"], "bullets_zh": ["中文五点 1", "中文五点 2", "中文五点 3", "中文五点 4", "中文五点 5"], "qa": [{"question": "English question", "answer": "English answer", "question_zh": "中文问题", "answer_zh": "中文回答"}], "internal_review_drafts": [{"title": "English review title", "title_zh": "中文评论标题", "content": "English buyer review", "content_zh": "中文买家评论"}]},
            "shopping_intents": [{"intent": "", "sample_queries": [], "evidence": []}],
            "risks": [],
        }
        prompt = (
            f"{selected_strategy['persona']} 当前采用【{selected_strategy['name']}】决策方案。"
            f"策略目标：{selected_strategy['objective']} 策略要求：{selected_strategy['instruction']}"
            "只能依据产品事实和SIF数据推断，禁止编造参数、认证、性能或绝对化承诺。消费者画像必须全部使用中文输出。标题、五点和Q&A必须先输出英文原文，并同时提供对应的中文翻译字段。"
            "internal_review_drafts 是模拟买家评论：假设自己已经买到并实际使用了该产品，依据产品事实和SIF证据，用第一人称、自然口吻评论购买原因、使用体验和仍需关注的地方。首次生成必须严格输出5条。每条评论必须包含 title、title_zh、content、content_zh 四个字段，其中 title/content 为英文，title_zh/content_zh 为中文翻译。不要添加‘内部模拟’、‘非真实评价’等免责声明前缀，不要编造未提供的体验或参数。返回严格JSON，不要Markdown。\n\n"
            "operator_notes 是运营人员自由填写的补充资料，可能混合已确认事实、运营策略偏好和待确认假设；必须区分三者。"
            "无法从资料确认的内容要放入risks或明确标记待验证，不能把运营笔记扩展成认证、医疗功效、性能保证或绝对化承诺。\n\n"
            f"产品事实：{json.dumps(product, ensure_ascii=False)}\n"
            f"运营已选定的核心卖点：{json.dumps(selected_points, ensure_ascii=False)}\n"
            f"SIF数据：{json.dumps(compact_sif_data(sif_data), ensure_ascii=False)}\n"
            f"输出结构：{json.dumps(schema_hint, ensure_ascii=False)}\n"
            "关键词必须按核心词、长尾词、功能词、场景词、人群词分类，并分为P0-P4。每个关键词尽量保留来源ASIN和证据。"
            "标题和五点英文原文使用产品站点对应语言；标题严格不超过75个字符；五点必须结合SIF数据突出不同卖点，并分别提供title_zh、bullets_zh中文翻译。Q&A同时提供英文question/answer和中文question_zh/answer_zh。"
            "internal_review_drafts 假设评论者已经购买并使用产品，使用第一人称和自然买家口吻，结合产品事实表达购买动机、使用感受和关注点；每条包含英文标题、中文标题、英文评论和中文翻译；不要添加免责声明或固定标签，不要冒充未验证的具体体验。"
            "如果事实不足，明确写入risks或unsupported信息，不要猜测。"
            "必须把运营已选定的核心卖点作为消费者画像、标题、五点、Q&A、模拟买家评论及购物场景的主要内容方向；不得擅自用未选择的方向替代。"
            "请先基于所选卖点、SIF证据和产品事实完成消费者画像，再根据画像和所选策略生成标题、五点、Q&A及购物场景。"
            "决策方案只用于你的内部分析视角，不要额外输出策略报告、策略输出栏或未经要求的字段；最终重点输出消费者画像、标题、五点、Q&A和购物场景。"
        )
        if regeneration_note:
            prompt += f"\n这是基于已有SIF数据的第{regeneration_note}次重新生成，只重新组织消费者画像、标题、五点、Q&A和购物场景，不要重新抓取SIF。"
        response, endpoint = await _chat_json_with_failover(
            messages=[
                {"role": "system", "content": "你输出可被程序解析的JSON。"},
                {"role": "user", "content": prompt},
            ],
            provider=gpt_provider,
            model=gpt_model,
        )
        content = response.choices[0].message.content or "{}"
        result = json.loads(content)
        # The selected mode is authoritative; do not trust a model-generated
        # strategy label that could disagree with the submitted request.
        result["strategy"] = selected_strategy
        result.setdefault("keywords", build_keywords(product, sif_data))
        result.setdefault("consumer_profiles", [])
        result.setdefault("content_strategy", {})
        result.setdefault("listing", {})
        result.setdefault("shopping_intents", [])
        result.setdefault("risks", [])
        result["selected_selling_points"] = selected_points
        result["listing"]["title"] = limit_title(result["listing"].get("title"))
        result["listing"]["title_zh"] = str(result["listing"].get("title_zh") or "").strip()
        result["listing"]["bullets"] = list(result["listing"].get("bullets") or [])[:5]
        result["listing"]["bullets_zh"] = list(result["listing"].get("bullets_zh") or [])[:5]
        qa_rows = []
        for item in list(result["listing"].get("qa") or []):
            if not isinstance(item, dict):
                continue
            qa_rows.append({
                **item,
                "question": str(item.get("question") or "").strip(),
                "answer": str(item.get("answer") or "").strip(),
                "question_zh": str(item.get("question_zh") or "").strip(),
                "answer_zh": str(item.get("answer_zh") or "").strip(),
            })
        result["listing"]["qa"] = qa_rows
        review_entries = [normalize_review_entry(item) for item in (result["listing"].get("internal_review_drafts") or [])]
        fallback_reviews = [normalize_review_entry(item) for item in (_local_result(product_for_fallback, sif_data, strategy).get("listing") or {}).get("internal_review_drafts", [])]
        existing_review_keys = {(item["title"], item["content"]) for item in review_entries}
        for fallback_review in fallback_reviews:
            if len(review_entries) >= 5:
                break
            key = (fallback_review["title"], fallback_review["content"])
            if key not in existing_review_keys:
                review_entries.append(fallback_review)
                existing_review_keys.add(key)
        result["listing"]["internal_review_drafts"] = [
            item for item in review_entries[:5] if item["content"] or item["content_zh"]
        ]
        result["meta"] = {
            "mode": (
                "openai-middleman"
                if endpoint["id"] == "middleman"
                else ("openai-official-fallback" if gpt_provider == "auto" else "openai-official")
            ),
            "model": endpoint["model"],
            "endpoint": endpoint["label"],
            "gpt_provider": gpt_provider,
            "gpt_provider_name": gpt_provider_info(gpt_provider)["name"],
            "timeout_seconds": _env_float("OPENAI_TIMEOUT_SECONDS", 120.0),
            "strategy": strategy,
        }
        if endpoint["id"] == "official" and gpt_provider == "auto":
            result["meta"]["fallback_used"] = True
            primary_base = os.getenv("OPENAI_BASE_URL", "").strip()
            result["meta"]["fallback_from"] = urlsplit(primary_base).netloc or "中转站"
        return result
    except Exception as exc:
        fallback = _local_result(product_for_fallback, sif_data, strategy)
        fallback["selected_selling_points"] = selected_points
        diagnostic = f"GPT调用失败（{type(exc).__name__}），已使用本地兜底分析：{exc}"
        fallback.setdefault("risks", []).insert(0, diagnostic)
        fallback["meta"]["mode"] = "local-fallback"
        fallback["meta"]["error_type"] = type(exc).__name__
        fallback["meta"]["gpt_provider"] = gpt_provider
        fallback["meta"]["model"] = gpt_model
        return fallback


async def regenerate_section(
    product: dict[str, Any],
    sif_data: dict[str, Any],
    analysis: dict[str, Any],
    section: str,
    qa_index: int | None = None,
    qa_count: int | None = None,
    review_count: int | None = None,
    strategy: str = "comprehensive",
    gpt_provider: str = "official",
    gpt_model: str = "gpt-5.6-sol",
) -> Any:
    """Regenerate one listing section from stored SIF data only."""
    allowed = {"title", "bullets", "qa", "reviews"}
    if section not in allowed:
        raise ValueError("不支持的内容类型")
    if section == "qa":
        current_qa = list((analysis.get("listing") or {}).get("qa") or [])
        if qa_index is not None and (qa_index < 0 or qa_index >= len(current_qa)):
            raise ValueError("Q&A索引无效")

    strategy = normalize_strategy(strategy)
    gpt_provider = normalize_gpt_provider(gpt_provider)
    gpt_model = str(gpt_model or "gpt-5.6-sol").strip() or "gpt-5.6-sol"
    selected_points = list(analysis.get("selected_selling_points") or [])
    if not _gpt_endpoints(gpt_provider, gpt_model):
        raise RuntimeError(
            f"所选GPT节点【{gpt_provider_info(gpt_provider)['name']}】未配置"
        )

    try:
        listing = analysis.get("listing") or {}
        current = (listing.get("qa", []) if qa_index is None else listing.get("qa", [])[qa_index]) if section == "qa" else listing
        if section == "title":
            output_hint = '{"title":"English title within 75 characters","title_zh":"中文标题翻译"}'
            instruction = "只生成一个英文亚马逊标题及对应中文翻译，必须结合SIF高相关词和产品事实；英文标题严格不超过75个字符。"
        elif section == "bullets":
            output_hint = '{"bullets":["English bullet 1","English bullet 2","English bullet 3","English bullet 4","English bullet 5"],"bullets_zh":["中文五点1","中文五点2","中文五点3","中文五点4","中文五点5"]}'
            instruction = "只生成五条英文五点及逐条对应的中文翻译，每条突出不同卖点，结合SIF数据中的高价值关键词和产品事实，不编造参数。"
        elif section == "qa":
            if qa_index is None:
                requested_count = max(1, min(50, int(qa_count or 5)))
                output_hint = '{"qa":[{"question":"English buyer question","answer":"English answer","question_zh":"中文问题","answer_zh":"中文回答"}]}'
                instruction = f"重新生成完整Q&A，严格输出{requested_count}条买家真实会问的问题和直接、具体、合规的回答；每条同时提供英文 question/answer 和中文 question_zh/answer_zh。"
            else:
                output_hint = '{"question":"English buyer question","answer":"English answer","question_zh":"中文问题","answer_zh":"中文回答"}'
                instruction = f"只重新生成第{qa_index + 1}条Q&A，问题要像买家真实会问的问题，回答要直接、具体、合规；同时提供英文和中文翻译。"
        else:
            output_hint = '{"internal_review_drafts":[{"title":"English review title","title_zh":"中文评论标题","content":"English buyer review","content_zh":"中文买家评论"}]}'
            requested_count = max(1, min(50, int(review_count or 5)))
            instruction = f"严格生成{requested_count}条模拟买家评论。假设评论者已经买到并使用该产品，使用第一人称和自然口吻，根据产品事实表达购买动机、使用感受、满意点和仍需关注的地方。每条必须包含 title、title_zh、content、content_zh 四个字段：标题和评论内容先用英文，随后提供对应中文翻译。不要添加‘内部模拟’或‘非真实评价’等免责声明前缀，不要编造参数或未提供的体验。"
        prompt = (
            f"你是亚马逊Listing内容优化助手，当前决策策略是【{strategy_info(strategy)['name']}】。{strategy_info(strategy)['instruction']}"
            "只能依据产品事实和SIF数据，禁止编造参数、认证、性能或绝对化承诺。"
            "策略仅作为内部人设和写作规则；最终只改写指定的标题、五点、Q&A或内部模拟反馈，不输出策略报告。标题、五点和Q&A必须同时保留英文原文与中文翻译。"
            "返回严格JSON，不要Markdown。\n\n"
            f"任务：{instruction}\n"
            f"产品事实：{json.dumps(product, ensure_ascii=False)}\n"
            f"运营已选定的核心卖点：{json.dumps(selected_points, ensure_ascii=False)}\n"
            f"SIF数据：{json.dumps(compact_sif_data(sif_data), ensure_ascii=False)}\n"
            f"当前内容（仅作改写参考）：{json.dumps(current, ensure_ascii=False)}\n"
            f"输出结构：{output_hint}\n"
        )
        response, _endpoint = await _chat_json_with_failover(
            messages=[
                {"role": "system", "content": "你输出可被程序解析的JSON。"},
                {"role": "user", "content": prompt},
            ],
            provider=gpt_provider,
            model=gpt_model,
        )
        generated = json.loads(response.choices[0].message.content or "{}")
        if section == "title":
            value = {"title": limit_title(generated.get("title")), "title_zh": str(generated.get("title_zh") or "").strip()}
            if not value["title"]:
                raise RuntimeError("GPT返回的标题为空")
            return value
        if section == "bullets":
            bullets = [str(item).strip() for item in (generated.get("bullets") or []) if str(item).strip()]
            bullets_zh = [str(item).strip() for item in (generated.get("bullets_zh") or []) if str(item).strip()]
            if not bullets:
                raise RuntimeError("GPT返回的五点内容为空")
            return {"bullets": bullets[:5], "bullets_zh": bullets_zh[:5]}
        if section == "qa":
            if qa_index is None:
                items = generated.get("qa") or []
                qa = [
                    {"question": str(item.get("question") or "").strip(), "answer": str(item.get("answer") or "").strip(), "question_zh": str(item.get("question_zh") or "").strip(), "answer_zh": str(item.get("answer_zh") or "").strip()}
                    for item in items
                    if isinstance(item, dict) and (item.get("question") or item.get("answer"))
                ]
                if not qa:
                    raise RuntimeError("GPT返回的Q&A为空")
                return qa[:50]
            item = generated.get("qa") if isinstance(generated.get("qa"), dict) else generated
            if not item.get("question") and not item.get("answer"):
                raise RuntimeError("GPT返回的Q&A为空")
            return {
                "question": str(item.get("question") or "").strip(),
                "answer": str(item.get("answer") or "").strip(),
                "question_zh": str(item.get("question_zh") or "").strip(),
                "answer_zh": str(item.get("answer_zh") or "").strip(),
            }
        raw_reviews = generated.get("internal_review_drafts") or generated.get("reviews") or []
        reviews = [normalize_review_entry(item) for item in raw_reviews]
        reviews = [item for item in reviews if item["content"] or item["content_zh"]]
        if not reviews:
            raise RuntimeError("GPT返回的模拟买家评论为空")
        return reviews[:50]
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc).startswith("GPT返回"):
            raise
        raise RuntimeError(f"GPT重新生成请求失败：{exc}") from exc
