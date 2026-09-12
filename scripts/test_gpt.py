r"""Standalone SIF -> GPT integration test.

Examples (PowerShell):
    .\.venv\Scripts\python.exe scripts\test_gpt.py --gpt-only
    .\.venv\Scripts\python.exe scripts\test_gpt.py --asins B0AAA... B0BBB... B0CCC...

The script never prints the full API key.  It first probes the configured
OpenAI-compatible endpoint, then runs the same ``analyze()`` function used by
the web app so the Listing title, bullets and Q&A are generated from the SIF
payload rather than from a separate test prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env", override=True)

from backend.analysis import analyze, compact_sif_data  # noqa: E402
from backend.sif_mcp import SifMcpClient, SifMcpError, demo_data  # noqa: E402


def _safe_url(value: str) -> str:
    parts = urlsplit(value or "")
    if not parts.scheme or not parts.netloc:
        return "INVALID_URL"
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _mask_key(value: str) -> str:
    if not value:
        return "EMPTY"
    # Do not print even a prefix/suffix: command output can be copied into
    # logs or screenshots.  Length is enough to confirm that a value loaded.
    return f"SET(length={len(value)})"


def _env_float(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测试 SIF → GPT → Listing 标题/五点/Q&A 链路")
    parser.add_argument("--asins", nargs="+", help="真实测试 ASIN，完整 SIF 流程需要 3-10 个")
    parser.add_argument("--country", default="US", help="站点代码，默认 US")
    parser.add_argument("--gpt-only", action="store_true", help="跳过 SIF 网络请求，使用本地演示 SIF 数据测试 GPT")
    parser.add_argument("--demo-sif", action="store_true", help="跳过真实 SIF，使用演示 SIF 数据，但仍真实调用 GPT")
    parser.add_argument("--skip-probe", action="store_true", help="跳过最小 GPT 连通性测试")
    parser.add_argument("--official-gpt", action="store_true", help="额外测试官网 OpenAI API；使用 OPENAI_OFFICIAL_API_KEY")
    parser.add_argument("--probe-only", action="store_true", help="只做中转站/官网连通性检测，不执行 SIF→GPT 内容生成")
    parser.add_argument("--timeout", type=float, help="覆盖 OPENAI_TIMEOUT_SECONDS")
    parser.add_argument("--product-name", default="Massage Headrest for Bed, Adjustable Face Cradle with Memory Foam Pillow")
    parser.add_argument("--brand", default="")
    parser.add_argument("--category", default="Massage Tables & Chairs")
    parser.add_argument("--material", default="绵棉")
    parser.add_argument("--specifications", default="")
    parser.add_argument("--functions", default="脸部支撑、可调节、记忆棉枕垫")
    parser.add_argument("--packaging", default="")
    parser.add_argument("--selling-points", default="")
    parser.add_argument("--output", help="可选：将测试摘要保存为 JSON 文件")
    return parser.parse_args()


def product_from_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "product_name": args.product_name,
        "brand": args.brand,
        "category": args.category,
        "material": args.material,
        "specifications": args.specifications,
        "functions": args.functions,
        "packaging": args.packaging,
        "selling_points": args.selling_points,
        "target_customer": "",
        "claims_allowed": "",
        "claims_forbidden": "",
    }


async def probe_endpoint(api_key: str, base_url: str | None, model: str, timeout: float) -> dict[str, object]:
    """Make a tiny JSON request through an OpenAI-compatible endpoint."""
    from openai import AsyncOpenAI

    if not api_key:
        raise RuntimeError("API Key 未配置")
    client_kwargs: dict[str, object] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": 0,
    }
    if base_url:
        client_kwargs["base_url"] = base_url
    client = AsyncOpenAI(**client_kwargs)
    started = time.monotonic()
    try:
        response = await client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": 'Return exactly {"ok": true}.'},
            ],
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        return {
            "ok": bool(parsed.get("ok") is True),
            "seconds": round(time.monotonic() - started, 2),
            "model": response.model,
            "response_format": "json_object",
        }
    finally:
        await client.close()


async def probe_gpt(timeout: float) -> dict[str, object]:
    """Probe the configured middleman endpoint."""
    return await probe_endpoint(
        os.getenv("OPENAI_API_KEY", "").strip(),
        os.getenv("OPENAI_BASE_URL", "").strip() or None,
        os.getenv("OPENAI_MODEL", "gpt-5-mini").strip(),
        timeout,
    )


async def probe_official_gpt(timeout: float) -> dict[str, object]:
    """Probe api.openai.com without changing the middleman configuration."""
    return await probe_endpoint(
        os.getenv("OPENAI_OFFICIAL_API_KEY", "").strip(),
        os.getenv("OPENAI_OFFICIAL_BASE_URL", "https://api.openai.com/v1").strip(),
        os.getenv("OPENAI_OFFICIAL_MODEL", "gpt-5-mini").strip(),
        timeout,
    )


async def probe_official_reachability(timeout: float) -> dict[str, object]:
    """Check whether api.openai.com is reachable, even without an API key."""
    import httpx

    base_url = os.getenv("OPENAI_OFFICIAL_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.get(f"{base_url}/models")
    return {
        "reachable": response.status_code in {200, 401, 403},
        "status": response.status_code,
        "seconds": round(time.monotonic() - started, 2),
        "meaning": "200=官网Key有效；401=官网可达但未提供有效Key；403=官网可达但访问受限",
    }


async def collect_sif(args: argparse.Namespace, asins: list[str]) -> tuple[dict[str, object], str]:
    if args.gpt_only or args.demo_sif:
        return demo_data(asins, args.country.upper()), "demo"
    if len(asins) < 3 or len(asins) > 10:
        raise ValueError("完整 SIF 流程需要通过 --asins 传入 3-10 个 ASIN；若只测 GPT，请使用 --gpt-only")
    async with SifMcpClient() as client:
        return await client.collect(asins, args.country.upper(), top_n=50), "sif"


async def run(args: argparse.Namespace) -> int:
    base_url = os.getenv("OPENAI_BASE_URL", "").strip()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-5-mini").strip()
    timeout = args.timeout or _env_float("OPENAI_TIMEOUT_SECONDS", 120.0)
    official_key = os.getenv("OPENAI_OFFICIAL_API_KEY", "").strip()
    official_base = os.getenv("OPENAI_OFFICIAL_BASE_URL", "https://api.openai.com/v1").strip()
    official_model = os.getenv("OPENAI_OFFICIAL_MODEL", "gpt-5-mini").strip()
    asins = [item.strip().upper() for item in (args.asins or []) if item.strip()]
    if args.gpt_only and not asins:
        asins = ["DEMO000001", "DEMO000002", "DEMO000003"]

    print("=== GPT 配置 ===")
    print(f"请求根地址: {_safe_url(base_url)}")
    print(f"模型: {model or '未配置'}")
    print(f"API Key: {_mask_key(api_key)}")
    print(f"超时: {timeout:g} 秒")

    probe_failed = False
    if not args.skip_probe:
        print("\n=== 1/3 GPT 最小请求测试 ===")
        try:
            probe = await probe_gpt(timeout)
            print(json.dumps(probe, ensure_ascii=False))
            if not probe.get("ok"):
                print("警告：请求成功但返回 JSON 中没有 ok=true")
        except Exception as exc:
            print(f"失败: {type(exc).__name__}: {exc}")
            probe_failed = True
    else:
        print("\n=== 1/3 GPT 最小请求测试 ===\n已跳过")

    if args.official_gpt:
        print("\n=== 官网 OpenAI GPT 连通性测试 ===")
        print(f"请求根地址: {_safe_url(official_base)}")
        print(f"模型: {official_model or '未配置'}")
        print(f"API Key: {_mask_key(official_key)}")
        try:
            reachability = await probe_official_reachability(min(timeout, 20.0))
            print("官网网络: " + json.dumps(reachability, ensure_ascii=False))
        except Exception as exc:
            print(f"官网网络失败: {type(exc).__name__}: {exc}")
        if not official_key:
            print("官网模型请求跳过：未配置 OPENAI_OFFICIAL_API_KEY")
        else:
            try:
                official_probe = await probe_official_gpt(timeout)
                print(json.dumps(official_probe, ensure_ascii=False))
            except Exception as exc:
                print(f"失败: {type(exc).__name__}: {exc}")

    if args.probe_only:
        print("\n已完成连通性检测，按 --probe-only 跳过 SIF→GPT 内容生成。")
        return 2 if probe_failed else 0

    print("\n=== 2/3 SIF 数据准备 ===")
    try:
        sif_data, source = await collect_sif(args, asins)
    except (SifMcpError, ValueError, Exception) as exc:
        print(f"失败: {type(exc).__name__}: {exc}")
        return 3
    compact = compact_sif_data(sif_data)
    item_count = len(sif_data.get("items", [])) if isinstance(sif_data, dict) else 0
    profile_count = len((sif_data.get("profiles") or {}).get("list", [])) if isinstance(sif_data, dict) and isinstance(sif_data.get("profiles"), dict) else 0
    compact_bytes = len(json.dumps(compact, ensure_ascii=False).encode("utf-8"))
    print(f"来源: {source}; ASIN 数: {item_count}; 画像记录: {profile_count}; 发送给 GPT 的压缩 SIF 数据: {compact_bytes} bytes")
    if source == "demo":
        print("提示：这是演示 SIF 数据，不代表实时竞品结果；GPT 请求仍是真实请求。")

    print("\n=== 3/3 SIF → GPT → Listing ===")
    started = time.monotonic()
    result = await analyze(product_from_args(args), sif_data)
    elapsed = round(time.monotonic() - started, 2)
    listing = result.get("listing") or {}
    bullets = listing.get("bullets") if isinstance(listing, dict) else []
    qa = listing.get("qa") if isinstance(listing, dict) else []
    meta = result.get("meta") or {}
    risks = result.get("risks") or []
    print(f"耗时: {elapsed:g} 秒")
    print(
        f"GPT 模式: {meta.get('mode', 'unknown')}; "
        f"模型: {meta.get('model', model)}; 节点: {meta.get('endpoint', _safe_url(base_url))}"
    )
    print(f"标题: {listing.get('title', '') if isinstance(listing, dict) else ''}")
    if isinstance(listing, dict):
        print(f"标题中文翻译: {listing.get('title_zh', '')}")
    print("五点:")
    bullets_zh = listing.get("bullets_zh") if isinstance(listing, dict) else []
    for index, bullet in enumerate(bullets if isinstance(bullets, list) else [], 1):
        print(f"  {index}. {bullet}")
        if isinstance(bullets_zh, list) and index <= len(bullets_zh):
            print(f"     中文: {bullets_zh[index - 1]}")
    print("Q&A:")
    for row in qa if isinstance(qa, list) else []:
        if isinstance(row, dict):
            print(f"  Q: {row.get('question', '')}")
            print(f"  A: {row.get('answer', '')}")
            print(f"  中文问: {row.get('question_zh', '')}")
            print(f"  中文答: {row.get('answer_zh', '')}")
    reviews = listing.get("internal_review_drafts") if isinstance(listing, dict) else []
    print("模拟买家评论:")
    for index, review in enumerate(reviews if isinstance(reviews, list) else [], 1):
        if isinstance(review, dict):
            print(f"  {index}. 标题: {review.get('title', '')}")
            print(f"     中文标题: {review.get('title_zh', '')}")
            print(f"     评论: {review.get('content', '')}")
            print(f"     中文翻译: {review.get('content_zh', '')}")
        else:
            print(f"  {index}. {review}")
    if risks:
        print("风险/诊断:")
        for risk in risks[:3]:
            print(f"  - {risk}")

    summary = {
        "config": {"base_url": _safe_url(base_url), "model": model, "api_key_set": bool(api_key), "timeout_seconds": timeout},
        "sif": {"source": source, "asins": asins, "item_count": item_count, "profile_count": profile_count, "compact_bytes": compact_bytes},
        "analysis": {"elapsed_seconds": elapsed, "meta": meta, "listing": listing, "risks": risks},
    }
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = ROOT / output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"测试摘要已保存: {output_path}")
    if meta.get("mode") not in {"openai", "openai-official", "openai-official-fallback"}:
        return 4
    return 2 if probe_failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("\n已取消")
        raise SystemExit(130)
