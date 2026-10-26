import os
import re
import csv
import io
import json
import urllib.parse
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient

app = FastAPI()

# 1. 환경변수
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
raw_keys = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY"),
]
GEMINI_API_KEYS = list(dict.fromkeys([k.strip() for k in raw_keys if k and k.strip()]))
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# 2. 마스터 지식 시트 캐싱 (1,347건)
MASTER_SHEET_ID = "1k-eGv2EFu4GG3H3oKG-Ql9IF-UVD_kZMTjnUiyLqLp4"
CSV_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{MASTER_SHEET_ID}/gviz/tq?tqx=out:csv&sheet=%F0%9F%93%9AAI_%EB%A7%88%EC%8A%A4%ED%84%B0%EC%A7%80%EC%8B%9D"

CACHED_KNOWLEDGE = []

def sync_master_knowledge():
    global CACHED_KNOWLEDGE
    try:
        res = requests.get(CSV_EXPORT_URL, timeout=12)
        if res.status_code == 200:
            f = io.StringIO(res.text)
            reader = csv.reader(f)
            rows = list(reader)
            
            new_data = []
            for r in rows[1:]:
                if len(r) >= 4:
                    cat = r[0].strip() if len(r) > 0 else ""
                    sub = r[1].strip() if len(r) > 1 else ""
                    q = r[2].strip() if len(r) > 2 else ""
                    a = r[3].strip() if len(r) > 3 else ""
                    kw = r[4].strip() if len(r) > 4 else ""

                    if q and a:
                        kw_list = [k.strip().lower() for k in kw.split(",") if k.strip()]
                        kw_list.extend(re.findall(r"[\w가-힣]+", q.lower()))
                        new_data.append({
                            "category": cat,
                            "sub": sub,
                            "question": q,
                            "answer": a,
                            "keywords": list(set(kw_list))
                        })

            if new_data:
                CACHED_KNOWLEDGE = new_data
                print(f"✅ 마스터 지식 로드 성공: 총 {len(CACHED_KNOWLEDGE)}건 캐싱 완료")
                return len(CACHED_KNOWLEDGE)
    except Exception as e:
        print(f"⚠️ 시트 로드 예외: {e}")
    return len(CACHED_KNOWLEDGE)

sync_master_knowledge()

def search_memory_knowledge(query: str):
    clean_q = re.sub(r"[^\w\s가-힣]", " ", query.lower())
    stop_words = {"서류", "필요", "뭐야", "알려줘", "관련", "대해", "어떻게", "확인", "부탁"}
    tokens = [t for t in clean_q.split() if len(t) > 1 and t not in stop_words]

    scored = []
    for item in CACHED_KNOWLEDGE:
        score = 0
        sub_lower = item["sub"].lower()
        q_lower = item["question"].lower()

        for token in tokens:
            if token in sub_lower:
                score += 50
            if token in q_lower:
                score += 30
            for kw in item["keywords"]:
                if token == kw:
                    score += 20
                elif token in kw:
                    score += 5

        if score >= 20:
            content = f"[{item['category']} > {item['sub']} - {item['question']}]\n{item['answer']}"
            scored.append((score, content))

    scored.sort(key=lambda x: x[0], reverse=True)
    matched_items = [s[1] for s in scored[:2]]
    return "\n\n---\n\n".join(matched_items), len(matched_items) > 0

def call_gemini_api(prompt: str) -> str:
    models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048}
    }

    for model in models:
        for key in GEMINI_API_KEYS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            try:
                res = requests.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=15
                )
                if res.status_code == 200:
                    data = res.json()
                    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"]
            except Exception:
                continue
    return ""

def build_escalation_blocks(reply_text: str, channel_id: str, thread_ts: str, question: str):
    # 모달 생성 시 질문 원문을 전달하기 위한 메타데이터
    meta_val = json.dumps({"c": channel_id, "t": thread_ts, "q": question[:80]}, ensure_ascii=False)
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": reply_text}
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚨 관리자(SV) 확인 요청", "emoji": True},
                    "style": "danger",
                    "action_id": "open_escalate_modal",
                    "value": meta_val
                }
            ]
        }
    ]

def async_bot_worker(channel_id: str, thread_ts: str, message_ts: str, question: str, is_thread_followup: bool = False):
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass

    direct_escalate_keywords = ["관리자", "sv", "담당자", "인계", "연결해"]
    is_direct_escalate = any(k in question.lower() for k in direct_escalate_keywords)
    correction_keywords = ["틀렸", "달라", "아닌", "다시", "확인해", "오답", "이상해"]
    is_correction = any(k in question.lower() for k in correction_keywords)

    evidence, has_match = search_memory_knowledge(question)

    prompt = f"""당신은 고객센터 상담사를 실시간으로 돕는 전문 AI 어시스턴트입니다.
제공된 [사내 공식 규정]을 바탕으로 명확하게 가공 및 정제하여 작성하세요.
절대로 원본 데이터를 복사-붙여넣기 하듯 투박하게 출력하지 마세요.

[상담사 질문]: {question}

[사내 공식 규정]:
{evidence if has_match else "사내 규정에 명시되어 있지 않음"}

[반드시 준수할 정제 출력 서식]:
📢 *[핵심 답변]*:
(결론 및 필수 구비서류 목록을 보기 쉽게 번호나 글머리 기호로 정돈)

💡 *[상세 기준]*:
- **진행 프로세스**: 서류 접수 및 전산 처리 절차
- **대체 가능 서류**: 대체 서류 기준 및 유효기간 등

📌 *[상담 가이드 & 팁]*:
- 상담 시 필수 확인 사항 및 안내 꿀팁

(※ 규정이 없는 경우 '사내 공식 지식에 미등록된 항목이므로 SV 확인이 필요합니다.' 명시)"""

    ai_reply = call_gemini_api(prompt)

    need_button = False
    if is_direct_escalate or is_correction or not has_match or not ai_reply:
        need_button = True

    if ai_reply and ai_reply.strip():
        final_text = ai_reply
    elif has_match:
        final_text = (
            f"📢 *[핵심 답변]*:\n사내 마스터 규정 조회 결과입니다.\n\n"
            f"{evidence}\n\n"
            f"📌 *[상담 가이드 & 팁]*:\n- 최신 업무 지침 기준에 맞춰 상담을 진행해 주세요."
        )
    else:
        final_text = "📢 *[핵심 답변]*:\n사내 공식 지식에 등록되지 않은 항목입니다.\n\n📌 *[상담 가이드 & 팁]*:\n- 정확한 안내를 위해 아래 버튼을 눌러 SV(관리자) 확인을 진행해 주세요."

    try:
        if need_button:
            blocks = build_escalation_blocks(final_text, channel_id, thread_ts, question)
            slack_client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, blocks=blocks, text=final_text)
        else:
            slack_client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=final_text, mrkdwn=True)
    except Exception as e:
        print(f"❌ 슬랙 전송 실패: {e}")

    try:
        slack_client.reactions_remove(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")
    except Exception:
        pass

@app.get("/")
def health_check():
    return {"status": "ok", "loaded_knowledge_count": len(CACHED_KNOWLEDGE)}

@app.get("/sync")
def manual_sync():
    count = sync_master_knowledge()
    return {"status": "synced", "count": count}

@app.post("/slack/events")
async def handle_slack_events(request: Request, background_tasks: BackgroundTasks):
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")

    # 1. 버튼 클릭 및 모달 팝업 상호작용 (URL-Encoded payload 파싱)
    if body_str.startswith("payload="):
        parsed = urllib.parse.parse_qs(body_str)
        raw_payload = parsed.get("payload", ["{}"])[0]
        payload = json.loads(raw_payload)
        p_type = payload.get("type")

        # A. [🚨 관리자(SV) 확인 요청] 버튼 클릭 시 모달 팝업 오픈
        if p_type == "block_actions":
            trigger_id = payload.get("trigger_id")
            action = payload.get("actions", [{}])[0]
            action_val = action.get("value", "{}")
            meta_dict = json.loads(action_val)
            orig_q = meta_dict.get("q", "문의 내용 없음")

            # 기존 관리자 인계 질의서 모달 폼 완벽 복원
            modal_view = {
                "type": "modal",
                "callback_id": "submit_escalate_modal",
                "private_metadata": action_val,
                "title": {"type": "plain_text", "text": "관리자(SV) 인계 질의서"},
                "submit": {"type": "plain_text", "text": "인계 요청 전송"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📋 *상담사 질문 내용:*\n>{orig_q}"
                        }
                    },
                    {"type": "divider"},
                    {
                        "type": "input",
                        "block_id": "reason_block",
                        "label": {"type": "plain_text", "text": "인계 사유 및 추가 문의 사항"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reason_input",
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "예: 지식 시트 기준과 금액이 다름 / 예외 케이스 처리 방법 확인 필요 / 추가 규정 확인 요청 등"
                            }
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "💡 *안내*: 제출 시 해당 스레드에 관리자 호출 알림이 등록됩니다."
                            }
                        ]
                    }
                ]
            }

            try:
                slack_client.views_open(trigger_id=trigger_id, view=modal_view)
            except Exception as e:
                print(f"❌ 모달 팝업 오류: {e}")
            return {"status": "ok"}

        # B. 모달 팝업 제출(Submit) 시 스레드에 정식 인계 티켓 카드 전송
        elif p_type == "view_submission":
            user_info = payload.get("user", {})
            user_name = user_info.get("name", "상담사")
            user_id = user_info.get("id", "")
            
            meta = json.loads(payload.get("view", {}).get("private_metadata", "{}"))
            channel_id = meta.get("c")
            thread_ts = meta.get("t")
            orig_q = meta.get("q", "")
            
            reason = payload.get("view", {}).get("state", {}).get("values", {}).get("reason_block", {}).get("reason_input", {}).get("value", "")

            notice_blocks = [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "🚨 [관리자(SV) 업무 확인 요청]", "emoji": True}
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*요청 상담사:*\n<@{user_id}> ({user_name})"},
                        {"type": "mrkdwn", "text": f"*상태:*\n검토 대기중 ⏳"}
                    ]
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*상담사 원본 질문:*\n>{orig_q}"}
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*상세 인계 사유:*\n{reason}"}
                },
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "📌 *관리자(SV) 안내*: 검토 후 본 스레드 댓글로 답변 또는 지침을 남겨주세요."}
                    ]
                }
            ]

            try:
                slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    blocks=notice_blocks,
                    text=f"🚨 [관리자 확인 요청] {orig_q}"
                )
            except Exception as e:
                print(f"❌ 인계 카드 전송 실패: {e}")

            return {"response_action": "clear"}

    # 2. 일반 슬랙 메시지 이벤트 (JSON 파싱)
    try:
        data = json.loads(body_str)
    except Exception:
        return {"status": "ignored"}

    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    event = data.get("event", {})
    if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        channel_id = event.get("channel")
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        if text.startswith("[질의-AI]"):
            question = text.replace("[질의-AI]", "").strip()
            background_tasks.add_task(
                async_bot_worker,
                channel_id,
                thread_ts if thread_ts else message_ts,
                message_ts,
                question,
                False
            )
        elif thread_ts and thread_ts != message_ts:
            correction_keywords = ["틀렸", "달라", "아닌", "다시", "확인해", "관리자", "sv", "왜", "어떻게"]
            if any(k in text for k in correction_keywords) or len(text) > 4:
                background_tasks.add_task(
                    async_bot_worker,
                    channel_id,
                    thread_ts,
                    message_ts,
                    text,
                    True
                )

    return {"status": "ok"}
