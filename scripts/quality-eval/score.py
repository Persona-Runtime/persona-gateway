# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28,<1", "pyyaml>=6,<7"]
# ///
"""골든셋 채점기 — run.py 결과(jsonl)에 규칙 지표와 LLM 심판 점수를 매겨 summary를 쓴다.

- 규칙 지표(LLM 없음): 금지 문자열 포함, 문장 수, 한글 외 문자 비율, 검색 recall@5·
  citation precision, unknown 문항의 모른다율.
- LLM 심판: OpenAI 호환 엔드포인트 하나(env `QUALITY_JUDGE_*`)에 루브릭·기대 사실·답변을 주고
  5축 1~5점 JSON을 받는다. 심판이 외부 API면 답변 원문이 그쪽으로 간다 — **합성 자료로만
  돌린다는 전제**에서만 허용한다(README).
- mock 결과(`mode=mock`)는 답변을 채점하지 않는다. 합성 고정 응답이라 점수가 의미 없다.
  검색은 mock에서도 실제 임베딩으로 돌므로 검색 지표만 남긴다.

출력: `results/summary-<label>.md`(유형별·전체 평균 표 + 문항별 표)와
`results/<입력 이름>.scored.jsonl`(문항별 원점수).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger("quality_eval.score")

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_QUESTIONS = EVAL_DIR / "questions.yaml"

# 모든 문항에 공통으로 나오면 안 되는 문자열 — v2 지시문이 금지한 메타 발언이다.
# 규칙 지표는 신호일 뿐이다(예: "AI라니 무슨 소리야"도 걸린다). 최종 판단은 심판 점수와 함께 본다.
COMMON_MUST_NOT = (
    "AI",
    "인공지능",
    "챗봇",
    "언어 모델",
    "자료에 따르면",
    "참고 자료",
    "설정",
)
TYPES = ("fact", "unknown", "followup", "style")
AXES = ("grounding", "no_fabrication", "character", "fluency", "length_format")
AXIS_LABELS = {
    "grounding": "근거 정확성",
    "no_fabrication": "지어냄 없음",
    "character": "캐릭터 일관성",
    "fluency": "한국어 자연스러움",
    "length_format": "길이·형식",
}
RECALL_AT = 5
# unknown 문항에서 "모른다/기억이 흐릿하다"고 답했는지 보는 표현. 같은 소형 모델을 심판으로 쓰면
# 지어낸 답에도 '지어냄 없음' 고점을 주는 일이 실측에서 나와(2026-10-01 baseline), LLM 없이
# 판정하는 보조 지표로 둔다. 표현이 있어도 뒤에서 지어낼 수 있으므로 상한 신호로만 읽는다.
ABSTAIN_MARKERS = (
    "모르",
    "몰라",
    "기억 안",
    "기억이 안",
    "기억이 흐릿",
    "흐릿",
    "글쎄",
    "생각이 안 나",
)
# 말투 예시 citation은 검색 정답 비교에서 뺀다 — 정답 조각은 사건·관계 본문이다.
SPEECH_CITATION_PREFIX = "말투 예시"

JUDGE_TIMEOUT_SECONDS = 120.0
JUDGE_MAX_TOKENS = 400

_SENTENCE_END_RE = re.compile(r"[.!?。…]+|\n+")
_HANGUL_RE = re.compile(r"[가-힣ㄱ-ㆎ]")


# --- 규칙 지표 ------------------------------------------------------------------


def count_sentences(answer: str) -> int:
    return len([part for part in _SENTENCE_END_RE.split(answer) if part.strip()])


def non_hangul_letter_ratio(answer: str) -> float:
    """글자(letter) 중 한글이 아닌 것의 비율. 숫자·문장부호·공백은 세지 않는다.

    중국어·영어가 섞이는 "언어 섞임"을 잡으려는 값이다. 고유명사 영문 한두 개도 올라가므로
    절대 기준이 아니라 버전 간 비교용이다.
    """
    letters = [char for char in answer if char.isalpha()]
    if not letters:
        return 0.0
    non_hangul = [char for char in letters if not _HANGUL_RE.match(char)]
    return len(non_hangul) / len(letters)


def cited_body_paths(citations: list[dict[str, str]]) -> list[str]:
    """citation title("사건 · 경로")에서 본문 조각의 heading_path만 순서대로 꺼낸다."""
    paths = []
    for citation in citations:
        title = citation["title"]
        if title.startswith(SPEECH_CITATION_PREFIX):
            continue
        _, separator, path = title.partition(" · ")
        paths.append(path if separator else title)
    return paths


def retrieval_scores(
    expected_chunks: list[str], citations: list[dict[str, str]]
) -> tuple[float | None, float | None]:
    """(recall@5, precision). 기대 조각이 없는 문항(unknown·style)은 (None, None).

    recall: 기대 heading_path 중 상위 5개 본문 citation에 들어간 비율.
    precision: 본문 citation 중 기대 heading_path에 속한 비율(같은 경로 조각 여러 개면 모두 정답).
    """
    if not expected_chunks:
        return None, None
    expected = set(expected_chunks)
    cited = cited_body_paths(citations)
    recall = len(expected & set(cited[:RECALL_AT])) / len(expected)
    precision = sum(1 for path in cited if path in expected) / len(cited) if cited else 0.0
    return recall, precision


def rule_metrics(record: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    answer = record.get("answer", "")
    forbidden = [*COMMON_MUST_NOT, *item.get("must_not", [])]
    recall, precision = retrieval_scores(
        item.get("expected_chunks", []), record.get("citations", [])
    )
    return {
        "must_not_hits": [word for word in forbidden if word in answer],
        "sentences": count_sentences(answer),
        "non_hangul_ratio": round(non_hangul_letter_ratio(answer), 4),
        "recall_at_5": recall,
        "citation_precision": precision,
        # unknown 문항만 값이 있다(그 밖은 None).
        "abstained": (
            any(marker in answer for marker in ABSTAIN_MARKERS)
            if item["type"] == "unknown"
            else None
        ),
    }


# --- LLM 심판 ---------------------------------------------------------------------

RUBRIC = """너는 한국어 캐릭터 대화의 품질을 채점하는 심판이다. 캐릭터는 바닷가 서점 직원 "민서"이고,
반말을 쓰며 상대를 "너"라고 부르고 "~거든", "~더라" 같은 어미와 "음,"으로 운을 떼는 말버릇이 있다.
아래 다섯 축을 각각 1~5점(정수)으로 매긴다.

1. grounding(근거 정확성): 기대 사실이 정확히 들어 있는가. 기대 사실이 없는 문항(자료에 없는 질문·
   인사)은 모른다고 하거나 자연스럽게 넘겼으면 5점, 단정한 사실이 틀렸으면 감점.
2. no_fabrication(지어냄 없음): 기대 사실과 캐릭터 설명에 없는 구체적 사실(날짜·이름·숫자·사건)을
   지어냈는가. 지어낸 것이 없으면 5점. 문항 유형이 unknown인데 그럴듯한 답을 지어냈으면 1점.
3. character(캐릭터 일관성): 1인칭 반말·호칭·어미·차분한 성격을 지켰는가. 자신을 AI·모델·챗봇이라고
   하거나 '자료'·'설정'을 언급하면 1~2점.
4. fluency(한국어 자연스러움): 한국어로만, 어색한 번역투나 다른 언어 섞임 없이 자연스러운가.
5. length_format(길이·형식): 2~4문장의 대화체인가. 목록·제목·이모지·장황함·되묻기가 있으면 감점.

반드시 아래 JSON 한 개만 출력한다. 다른 말은 쓰지 않는다.
{"grounding": n, "no_fabrication": n, "character": n, "fluency": n, "length_format": n, "reason": "한 문장"}"""


@dataclass(frozen=True)
class JudgeConfig:
    base_url: str
    model: str
    api_key: str | None

    @staticmethod
    def from_env() -> JudgeConfig | None:
        base_url = os.environ.get("QUALITY_JUDGE_BASE_URL")
        model = os.environ.get("QUALITY_JUDGE_MODEL")
        if not base_url or not model:
            return None
        return JudgeConfig(base_url.rstrip("/"), model, os.environ.get("QUALITY_JUDGE_API_KEY"))


def judge_prompt(record: dict[str, Any], item: dict[str, Any]) -> str:
    history_lines = [
        f"{turn['role']}: {turn['content']}" for turn in record.get("actual_history", [])
    ]
    expected = item.get("expected_facts") or ["(없음 — 자료에 없는 질문이거나 인사·감정 질문)"]
    return "\n".join(
        [
            f"문항 유형: {item['type']}",
            "앞 대화:",
            *(history_lines or ["(없음)"]),
            f"질문: {item['question']}",
            "기대 사실:",
            *[f"- {fact}" for fact in expected],
            "채점할 답변:",
            record.get("answer", ""),
        ]
    )


def parse_judge_json(text: str) -> dict[str, Any]:
    """심판 응답에서 첫 JSON 객체를 꺼내 5축이 1~5 정수인지 확인한다. 아니면 ValueError."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON 객체가 없다")
    parsed = json.loads(text[start : end + 1])
    scores = {}
    for axis in AXES:
        value = parsed.get(axis)
        if not isinstance(value, int) or not 1 <= value <= 5:
            raise ValueError(f"{axis} 값이 1~5 정수가 아니다")
        scores[axis] = value
    scores["reason"] = str(parsed.get("reason", ""))
    return scores


def _post_with_one_retry(
    client: httpx.Client, url: str, payload: dict[str, Any], headers: dict[str, str]
) -> httpx.Response:
    """전송 계층 오류(연결 끊김 등)에만 한 번 더 시도한다.

    공유 vLLM에서 응답 없이 연결이 끊기는 일이 실측 중에 있었다. 같은 요청을 한 번 더 보내도
    채점 결과는 바뀌지 않으므로 재시도한다. HTTP 상태 오류·형식 오류는 재시도하지 않고 그대로
    judge_error로 드러낸다.
    """
    try:
        return client.post(url, json=payload, headers=headers)
    except httpx.TransportError:
        return client.post(url, json=payload, headers=headers)


def judge(client: httpx.Client, config: JudgeConfig, prompt: str) -> dict[str, Any]:
    """심판 한 번. 실패는 숨기지 않고 `judge_error`로 돌려준다(0점·skip으로 바꾸지 않는다)."""
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    payload = {
        "model": config.model,
        "messages": [{"role": "system", "content": RUBRIC}, {"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": JUDGE_MAX_TOKENS,
    }
    try:
        response = _post_with_one_retry(
            client, f"{config.base_url}/v1/chat/completions", payload, headers
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return parse_judge_json(content)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as error:
        # 응답 본문은 남기지 않는다(답변 원문이 섞여 있다). 분류만 기록한다.
        return {"judge_error": type(error).__name__ + ": " + str(error)[:120]}


# --- 집계 -------------------------------------------------------------------------


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def aggregate(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    """문항 묶음 하나의 평균. 심판 실패·미실행 문항은 심판 평균에서 빠진다(개수는 따로 센다).

    앞 턴 실패로 본 질문을 하지 않은 문항(history_failed)은 규칙 지표에서도 뺀다. 빈 답·빈
    citation으로 집계하면 recall 0처럼 "검색이 틀렸다"는 잘못된 숫자가 섞이기 때문이다.
    """
    judged = [
        row["judge"] for row in rows if row.get("judge") and "judge_error" not in row["judge"]
    ]
    summary: dict[str, float | None] = {axis: mean([j[axis] for j in judged]) for axis in AXES}
    summary["judge_overall"] = mean([sum(j[axis] for axis in AXES) / len(AXES) for j in judged])
    summary["n"] = len(rows)
    rows = [row for row in rows if not row.get("history_failed")]
    summary["asked"] = len(rows)
    summary["judged"] = len(judged)
    summary["must_not_rate"] = mean([1.0 if row["rules"]["must_not_hits"] else 0.0 for row in rows])
    summary["sentences"] = mean([row["rules"]["sentences"] for row in rows])
    summary["non_hangul"] = mean([row["rules"]["non_hangul_ratio"] for row in rows])
    recalls = [
        row["rules"]["recall_at_5"] for row in rows if row["rules"]["recall_at_5"] is not None
    ]
    precisions = [
        row["rules"]["citation_precision"]
        for row in rows
        if row["rules"]["citation_precision"] is not None
    ]
    summary["recall_at_5"] = mean(recalls)
    summary["precision"] = mean(precisions)
    abstains = [
        row["rules"]["abstained"] for row in rows if row["rules"].get("abstained") is not None
    ]
    summary["abstain_rate"] = mean([1.0 if value else 0.0 for value in abstains])
    summary["ttft_ms"] = mean([row["ttft_ms"] for row in rows if row.get("ttft_ms") is not None])
    summary["total_ms"] = mean([row["total_ms"] for row in rows if row.get("total_ms") is not None])
    return summary


def render_summary(
    label: str, config: dict[str, Any], rows: list[dict[str, Any]], note: str
) -> str:
    header = (
        "| 유형 | 질문/전체 | 심판 | "
        + " | ".join(AXIS_LABELS[axis] for axis in AXES)
        + " | 심판 평균 | 금지어율 | 문장 수 | 비한글 | recall@5 | precision | 모른다율 | TTFT ms | 총 ms |"
    )
    divider = "|" + " --- |" * (header.count("|") - 1)
    lines = [
        f"# 골든셋 결과 — {label}",
        "",
        "실행 조건: " + ", ".join(f"`{key}={value}`" for key, value in config.items()),
        "",
        note,
        "",
        header,
        divider,
    ]
    groups = [(kind, [row for row in rows if row["type"] == kind]) for kind in TYPES]
    groups.append(("전체", rows))
    for name, group in groups:
        if not group:
            continue
        s = aggregate(group)
        lines.append(
            f"| {name} | {s['asked']}/{s['n']} | {s['judged']} | "
            + " | ".join(fmt(s[axis]) for axis in AXES)
            + f" | {fmt(s['judge_overall'])} | {fmt(s['must_not_rate'])} | {fmt(s['sentences'], 1)}"
            + f" | {fmt(s['non_hangul'], 3)} | {fmt(s['recall_at_5'])} | {fmt(s['precision'])}"
            + f" | {fmt(s['abstain_rate'])} | {fmt(s['ttft_ms'], 0)} | {fmt(s['total_ms'], 0)} |"
        )
    lines += [
        "",
        "## 문항별",
        "",
        "| id | 상태 | 심판(5축) | 금지어 | 문장 | recall@5 | 비고 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        verdict = row.get("judge") or {}
        if "judge_error" in verdict:
            scores, remark = "judge_error", verdict["judge_error"]
        elif verdict:
            scores, remark = (
                "/".join(str(verdict[axis]) for axis in AXES),
                verdict.get("reason", ""),
            )
        else:
            scores, remark = "-", ""
        if row.get("history_failed"):
            remark = "앞 턴 실패로 본 질문 미실행"
        lines.append(
            f"| {row['id']} | {row.get('status') or '-'} | {scores} | "
            f"{', '.join(row['rules']['must_not_hits']) or '-'} | {row['rules']['sentences']} | "
            f"{fmt(row['rules']['recall_at_5'])} | {remark.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


def render_retrieval_only(label: str, config: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """mock 결과용 summary. 답변 점수 없이 검색 지표(recall@5·precision)만 쓴다."""
    completed = sum(1 for row in rows if row.get("status") == "completed")
    lines = [
        f"# 골든셋 결과 — {label}",
        "",
        "실행 조건: " + ", ".join(f"`{key}={value}`" for key, value in config.items()),
        "",
        (
            f"mock 모드 결과라 답변은 채점하지 않는다(배선 확인용). 문항 {len(rows)}개 중 "
            f"완료 {completed}개. 검색은 실제 임베딩으로 돌았으므로 검색 지표만 남긴다."
        ),
        "",
        "| 유형 | n | recall@5 | precision |",
        "| --- | --- | --- | --- |",
    ]
    groups = [(kind, [row for row in rows if row["type"] == kind]) for kind in TYPES]
    groups.append(("전체", rows))
    for name, group in groups:
        if not group:
            continue
        s = aggregate(group)
        lines.append(f"| {name} | {s['n']} | {fmt(s['recall_at_5'])} | {fmt(s['precision'])} |")
    lines += ["", "## 문항별 검색", "", "| id | recall@5 | precision | 상위 본문 citation |"]
    lines.append("| --- | --- | --- | --- |")
    for row in rows:
        if row["rules"]["recall_at_5"] is None:
            continue
        top = ", ".join(cited_body_paths(row.get("citations", []))[:RECALL_AT])
        lines.append(
            f"| {row['id']} | {fmt(row['rules']['recall_at_5'])} | "
            f"{fmt(row['rules']['citation_precision'])} | {top.replace('|', '/')} |"
        )
    return "\n".join(lines) + "\n"


# --- 진입점 -----------------------------------------------------------------------


def load_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="골든셋 채점기")
    parser.add_argument("results", type=Path, help="run.py가 만든 results/*.jsonl")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--no-judge", action="store_true", help="규칙 지표만 계산한다")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    records = load_records(args.results)
    if not records:
        logger.error("결과가 비어 있다: %s", args.results)
        return 1
    items = {
        item["id"]: item for item in yaml.safe_load(args.questions.read_text("utf-8"))["questions"]
    }
    config = records[0]["config"]
    label = config["label"]
    summary_path = args.results.parent / f"summary-{label}.md"

    modes = {record.get("mode") for record in records}
    if "mock" in modes:
        # mock 답변은 고정 합성 문장이라 답변 점수(심판·금지어·길이)를 매기지 않는다. 검색은
        # mock에서도 실제 임베딩으로 돌기 때문에 검색 지표만 따로 남긴다.
        rows = [
            {**record, "rules": rule_metrics(record, items[record["id"]])} for record in records
        ]
        summary_path.write_text(render_retrieval_only(label, config, rows), encoding="utf-8")
        logger.info("mock 결과라 답변 채점을 건너뛰고 검색 지표만 썼다: %s", summary_path)
        return 0

    judge_config = None if args.no_judge else JudgeConfig.from_env()
    if not args.no_judge and judge_config is None:
        logger.error("QUALITY_JUDGE_BASE_URL·QUALITY_JUDGE_MODEL이 필요하다(또는 --no-judge)")
        return 2

    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=JUDGE_TIMEOUT_SECONDS) as client:
        for record in records:
            item = items[record["id"]]
            row = {**record, "rules": rule_metrics(record, item)}
            if judge_config is not None and record.get("status") == "completed":
                row["judge"] = judge(client, judge_config, judge_prompt(record, item))
            rows.append(row)
            logger.info("%s 채점 완료", record["id"])

    scored_path = args.results.with_suffix(".scored.jsonl")
    scored_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    judge_note = (
        f"심판: `{judge_config.model}` (OpenAI 호환). 같은 vLLM을 심판으로 쓰면 자기 편향이 있어 "
        "절대값이 아니라 라벨 간 상대 비교로만 읽는다."
        if judge_config is not None
        else "심판 미실행(--no-judge). 규칙 지표만 계산했다."
    )
    summary_path.write_text(render_summary(label, config, rows, judge_note), encoding="utf-8")
    logger.info("summary: %s", summary_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
