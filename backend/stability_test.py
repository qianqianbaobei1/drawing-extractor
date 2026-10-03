"""Compare repeated recognition of one PDF; each run calls the paid model API.

Usage: python stability_test.py drawing.pdf --runs 3
"""
import argparse
import difflib
import json

from extractor.assemble import assemble
from extractor.render import render_pdf
from extractor.schema import CONTRACT_VERSION, PROMPT_VERSION
from extractor.vision import VisionProvider


def canonical(value) -> str:
    return json.dumps(value.model_dump(), ensure_ascii=False, sort_keys=True, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if args.runs < 2:
        parser.error("--runs 至少为 2")

    provider = VisionProvider()
    if not provider.configured:
        parser.error("VISION_API_KEY 未配置")
    images = render_pdf(args.pdf)
    raw_outputs, assembled_outputs = [], []
    meta = {"model": provider.model, "prompt_version": PROMPT_VERSION,
            "contract_version": CONTRACT_VERSION}
    for index in range(args.runs):
        raw = provider.extract(images)
        raw_outputs.append(canonical(raw))
        assembled_outputs.append(canonical(assemble(raw, meta)))
        print(f"第 {index + 1}/{args.runs} 次完成")

    same_raw = len(set(raw_outputs)) == 1
    same_assembled = len(set(assembled_outputs)) == 1
    print(f"事实输出一致: {same_raw}; 组装结果一致: {same_assembled}")
    if not same_assembled:
        diff = difflib.unified_diff(
            assembled_outputs[0].splitlines(), assembled_outputs[1].splitlines(),
            fromfile="第1次", tofile="第2次", lineterm="",
        )
        print("\n".join(list(diff)[:80]))
    return 0 if same_raw and same_assembled else 1


if __name__ == "__main__":
    raise SystemExit(main())
