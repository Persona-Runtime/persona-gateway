# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28,<1", "pyyaml>=6,<7"]
# ///
"""골든셋 실행기 — 격리 스택의 Gateway에 실제 HTTP로 25문항을 묻고 결과를 jsonl로 남긴다.

흐름: 캐릭터 생성 → 초안(설정) → 자료 PATCH → apply(색인) → ready 대기 → activate →
문항마다 새 대화 → (후속 질문이면 history의 user 턴을 먼저 실제로 질문) → 질문 → SSE 수집.
끝나면 만든 캐릭터를 지운다(사용자당 캐릭터 3개 한도 때문에 남겨 두면 다음 실행이 막힌다).

운영에 돌리지 않는다. 대상 URL이 loopback·사설망 주소가 아니면 시작하지 않는다.
토큰은 env(`QUALITY_EVAL_TOKEN`)로만 받는다 — 명령줄 인자는 셸 기록·프로세스 목록에 남는다.

사용법은 README.md 참고.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

logger = logging.getLogger("quality_eval.run")

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_QUESTIONS = EVAL_DIR / "questions.yaml"
RESULTS_DIR = EVAL_DIR / "results"

TOKEN_ENV = "QUALITY_EVAL_TOKEN"
# 색인(apply)은 임베딩 서비스가 조각 수십 개를 처리하는 시간이다. CPU e5-small 기준 수 초지만
# 첫 기동 직후 모델 로딩이 겹치면 길어질 수 있어 넉넉히 둔다.
INDEXING_TIMEOUT_SECONDS = 300.0
INDEXING_POLL_SECONDS = 1.0
# 서비스 계약의 전체 생성 상한(180초)보다 조금 길게. 그 안에 done/error가 와야 한다.
CHAT_READ_TIMEOUT_SECONDS = 200.0
HTTP_TIMEOUT_SECONDS = 30.0


class EvalError(Exception):
    """실행을 계속할 수 없는 오류. 메시지에 토큰·자료 원문을 넣지 않는다."""


# --- 대상 안전 확인 ----------------------------------------------------------


def ensure_isolated_target(base_url: str) -> None:
    """loopback·사설망 주소만 허용한다. 운영 Gateway로 25문항을 보내는 실수를 막는다.

    호스트 이름은 localhost만 허용한다. 다른 이름은 DNS가 무엇을 가리킬지 여기서 알 수 없어
    거부한다(격리 스택은 eval-stack.sh가 127.0.0.1로 띄운다).
    """
    host = urlsplit(base_url).hostname
    if host is None:
        raise EvalError(f"base URL에서 호스트를 읽지 못했다: {base_url}")
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise EvalError(
            f"격리 스택이 아닐 수 있는 호스트 이름이라 중단한다: {host} "
            "(localhost 또는 사설 IP만 허용)"
        ) from error
    if not (address.is_loopback or address.is_private):
        raise EvalError(f"공인 주소라 중단한다: {host}")


# --- Gateway 호출 ------------------------------------------------------------


class Gateway:
    """이 실행기가 쓰는 Gateway API만 감싼다. 변경 요청마다 새 Idempotency-Key를 붙인다."""

    def __init__(self, base_url: str, token: str):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        self._client.close()

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Idempotency-Key": str(uuid.uuid4())} if method != "GET" else {}
        response = self._client.request(method, path, json=body, headers=headers)
        if response.status_code >= 400:
            # 오류 본문에는 계약상 code·message만 있다(자료 원문 없음).
            raise EvalError(f"{method} {path} → {response.status_code} {response.text[:300]}")
        if response.status_code == 204:
            return None
        return response.json()

    def create_persona(self, name: str) -> str:
        return self._send("POST", "/v1/personas", {"name": name})["id"]

    def delete_persona(self, persona_id: str) -> None:
        self._send("DELETE", f"/v1/personas/{persona_id}")

    def create_draft(self, persona_id: str, settings: dict[str, str]) -> int:
        draft = self._send("POST", f"/v1/personas/{persona_id}/draft", {"settings": settings})
        return draft["revision"]

    def add_sources(self, persona_id: str, revision: int, sources: list[dict[str, str]]) -> int:
        body = {"expected_revision": revision, "upsert_sources": sources}
        return self._send("PATCH", f"/v1/personas/{persona_id}/draft", body)["revision"]

    def apply(self, persona_id: str, revision: int) -> None:
        self._send(
            "POST", f"/v1/personas/{persona_id}/draft/apply", {"expected_revision": revision}
        )

    def get_draft(self, persona_id: str) -> dict[str, Any]:
        return self._send("GET", f"/v1/personas/{persona_id}/draft")

    def activate(self, persona_id: str, revision: int) -> None:
        self._send(
            "POST", f"/v1/personas/{persona_id}/draft/activate", {"expected_revision": revision}
        )

    def create_conversation(self, persona_id: str) -> str:
        return self._send("POST", f"/v1/personas/{persona_id}/conversations")["id"]

    def chat(self, conversation_id: str, message: str) -> ChatResult:
        """질문 하나를 보내고 SSE를 끝(done/error)까지 모은다."""
        started = time.monotonic()
        result = ChatResult()
        headers = {"Idempotency-Key": str(uuid.uuid4())}
        body = {"conversation_id": conversation_id, "message": message}
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, read=CHAT_READ_TIMEOUT_SECONDS)
        with self._client.stream(
            "POST", "/v1/chat/completions", json=body, headers=headers, timeout=timeout
        ) as response:
            if response.status_code != 200:
                response.read()
                raise EvalError(f"chat → {response.status_code} {response.text[:300]}")
            for event, data in iter_sse_events(response.iter_lines()):
                result.consume(event, data, elapsed_ms=(time.monotonic() - started) * 1000)
        result.total_ms = round((time.monotonic() - started) * 1000, 1)
        return result


def iter_sse_events(lines: Any) -> Any:
    """SSE 줄을 (event, data dict)로 묶는다. 빈 줄이 이벤트 경계다."""
    event_name = "message"
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data_lines:
                yield event_name, json.loads("\n".join(data_lines))
            event_name, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if data_lines:
        yield event_name, json.loads("\n".join(data_lines))


@dataclass
class ChatResult:
    answer_parts: list[str] = field(default_factory=list)
    citations: list[dict[str, str]] = field(default_factory=list)
    mode: str | None = None
    status: str | None = None
    failure_code: str | None = None
    ttft_ms: float | None = None
    total_ms: float | None = None
    # 응답 meta에 프롬프트 통계가 실리면 그대로 보관한다. 지금 Gateway는 싣지 않는다.
    prompt_stats: dict[str, Any] | None = None

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)

    def consume(self, event: str, data: dict[str, Any], *, elapsed_ms: float) -> None:
        if event == "meta":
            self.mode = data.get("mode")
            self.prompt_stats = data.get("prompt_stats")
        elif event == "citations":
            self.citations = [
                {"chunk_id": item["id"], "title": item["title"]} for item in data["items"]
            ]
        elif event == "delta":
            if self.ttft_ms is None:
                self.ttft_ms = round(elapsed_ms, 1)
            self.answer_parts.append(data["text"])
        elif event == "done":
            self.status = "completed"
        elif event == "error":
            self.status = data.get("status", "failed")
            self.failure_code = data.get("code")


# --- 캐릭터 준비 ---------------------------------------------------------------


def read_fixture(character_dir: Path, filename: str) -> str:
    return (character_dir / filename).read_text(encoding="utf-8")


def prepare_character(gateway: Gateway, character_dir: Path, persona_name: str) -> str:
    """합성 캐릭터를 만들고 색인·활성화까지 끝낸 뒤 persona id를 돌려준다."""
    persona_id = gateway.create_persona(persona_name)
    speech = read_fixture(character_dir, "speech_examples.txt")
    revision = gateway.create_draft(
        persona_id,
        {
            "name": "민서",
            "profile": read_fixture(character_dir, "profile.md").strip(),
            "speech_examples": speech,
        },
    )
    # 말투 검색은 speech_examples **소스**의 조각을 쓴다. 설정의 speech_examples와 같은 글을
    # 소스로도 넣는다(웹 클라이언트와 같은 방식).
    sources = [
        {
            "kind": "events",
            "filename": "events.md",
            "content": read_fixture(character_dir, "events.md"),
        },
        {
            "kind": "relationships",
            "filename": "relationships.md",
            "content": read_fixture(character_dir, "relationships.md"),
        },
        {"kind": "speech_examples", "filename": "speech_examples.txt", "content": speech},
    ]
    revision = gateway.add_sources(persona_id, revision, sources)
    gateway.apply(persona_id, revision)
    wait_until_indexed(gateway, persona_id)
    gateway.activate(persona_id, revision)
    return persona_id


def wait_until_indexed(gateway: Gateway, persona_id: str) -> None:
    deadline = time.monotonic() + INDEXING_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        draft = gateway.get_draft(persona_id)
        if draft["status"] == "ready" and not draft["requires_processing"]:
            return
        if draft["status"] == "failed":
            raise EvalError("색인이 실패했다(임베딩 서비스 로그를 확인)")
        time.sleep(INDEXING_POLL_SECONDS)
    raise EvalError(f"색인이 {INDEXING_TIMEOUT_SECONDS:.0f}초 안에 끝나지 않았다")


# --- 문항 실행 -----------------------------------------------------------------


def run_question(gateway: Gateway, persona_id: str, item: dict[str, Any]) -> dict[str, Any]:
    """문항 하나 = 대화 하나. 후속 질문은 history의 user 턴을 같은 대화에서 먼저 묻는다.

    assistant 턴은 API로 주입할 수 없으므로 모델이 실제로 한 답을 기록한다(actual_history).
    앞 턴이 실패하면 이력 없이 본 질문을 묻는 것과 같아져 비교가 깨지므로 그 문항을 실패로 둔다.
    """
    conversation_id = gateway.create_conversation(persona_id)
    actual_history: list[dict[str, str]] = []
    for turn in item.get("history", []):
        if turn["role"] != "user":
            continue
        warmup = gateway.chat(conversation_id, turn["content"])
        actual_history.append({"role": "user", "content": turn["content"]})
        actual_history.append({"role": "assistant", "content": warmup.answer})
        if warmup.status != "completed":
            # 실패 원인(failure_code)을 남겨야 baseline·비교 결과에서 이 문항이 왜 빠졌는지 읽힌다.
            return {
                "history_failed": True,
                "status": warmup.status,
                "failure_code": warmup.failure_code,
                "actual_history": actual_history,
            }

    result = gateway.chat(conversation_id, item["question"])
    return {
        "answer": result.answer,
        "citations": result.citations,
        "mode": result.mode,
        "status": result.status,
        "failure_code": result.failure_code,
        "ttft_ms": result.ttft_ms,
        "total_ms": result.total_ms,
        "prompt_stats": result.prompt_stats,
        "actual_history": actual_history,
    }


def load_questions(path: Path) -> tuple[Path, list[dict[str, Any]]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    character_dir = (path.parent / document["character_dir"]).resolve()
    return character_dir, document["questions"]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="답변 품질 골든셋 실행기(격리 스택 전용)")
    parser.add_argument(
        "--base-url", required=True, help="격리 Gateway 주소(예: http://127.0.0.1:18090)"
    )
    parser.add_argument("--label", required=True, help="결과 라벨(예: v2-b4096-s07)")
    # 아래 넷은 Gateway가 응답으로 알려주지 않는 실행 조건이다. 결과 해석에 필요해 함께 남긴다.
    parser.add_argument(
        "--prompt-version", required=True, help="Gateway에 준 PERSONA_PROMPT_VERSION"
    )
    parser.add_argument("--budget", required=True, help="프롬프트 예산 프로파일(예: 4096)")
    parser.add_argument("--sampling", required=True, help="샘플링 라벨(예: default, s07)")
    parser.add_argument("--model", required=True, help="vLLM served model 이름 또는 mock")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--only", nargs="*", help="이 id만 실행(배선 확인용)")
    parser.add_argument("--keep-persona", action="store_true", help="끝나도 캐릭터를 지우지 않는다")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        logger.error("%s 환경변수가 필요하다", TOKEN_ENV)
        return 2
    ensure_isolated_target(args.base_url)

    character_dir, questions = load_questions(args.questions)
    if args.only:
        questions = [item for item in questions if item["id"] in set(args.only)]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RESULTS_DIR.mkdir(exist_ok=True)
    output_path = RESULTS_DIR / f"{timestamp}-{args.label}.jsonl"
    config = {
        "label": args.label,
        "prompt_version": args.prompt_version,
        "budget_profile": args.budget,
        "sampling": args.sampling,
        "model": args.model,
    }

    gateway = Gateway(args.base_url, token)
    persona_id: str | None = None
    try:
        persona_id = prepare_character(gateway, character_dir, f"골든셋 민서 {timestamp}")
        logger.info("캐릭터 준비 완료 persona_id=%s 문항 %d개", persona_id, len(questions))
        with output_path.open("w", encoding="utf-8") as output:
            for item in questions:
                outcome = run_question(gateway, persona_id, item)
                record = {
                    "id": item["id"],
                    "type": item["type"],
                    "question": item["question"],
                    **outcome,
                    "config": config,
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                logger.info(
                    "%s status=%s ttft_ms=%s total_ms=%s",
                    item["id"],
                    outcome.get("status"),
                    outcome.get("ttft_ms"),
                    outcome.get("total_ms"),
                )
    except EvalError as error:
        logger.error("중단: %s", error)
        return 1
    finally:
        if persona_id is not None and not args.keep_persona:
            try:
                gateway.delete_persona(persona_id)
            except EvalError as error:
                # 정리 실패는 결과를 무효로 만들지 않는다. 다음 실행 전에 직접 지워야 함을 알린다.
                logger.warning(
                    "캐릭터 삭제 실패(직접 정리 필요) persona_id=%s: %s", persona_id, error
                )
        gateway.close()

    logger.info("결과: %s", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
