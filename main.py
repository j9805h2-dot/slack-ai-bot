import os
import re
import csv
import io
import json
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient

app = FastAPI()

# 1. 슬랙 및 API 환경변수 로드
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
raw_keys = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY"),
]
GEMINI_API_KEYS = list(dict.fromkeys([k.strip() for k in raw_keys if k and k.strip()]))

slack_client = WebClient(token=SLACK_BOT_TOKEN)

# 2. 독립형 마스터 지식 시트 캐싱 (1,347건)
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
    """키워드 가중치 기반 고속 색인 탐색"""
    clean_q = re.sub(r"[^\w\s가-힣]", " ", query.lower())
    tokens = [t for t in clean_q.split() if len(t) > 1]

    scored = []
    for item in CACHED_KNOWLEDGE:
        score = 0
        for token in tokens:
            for kw in item["keywords"]:
                if token in kw or kw in token:
                    score += 6
            if token in item["question"].lower():
                score += 18

        if score > 0:
            scored.append((score, f"[{item['category']} > {item['sub']} - {item['question']}]\n{item['answer']}"))

    scored.sort(key=lambda x: x[0], reverse=True)
    matched_text = "\n\n---\n\n".join([item[1] for item in scored[:3]])
    return matched_text, len(scored) > 0

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
    """관리자 인계 버튼이 포함된 슬랙 블록 생성"""
    meta_val = json.dumps({"c": channel_id, "t": thread_ts, "q": question[:40]}, ensure_ascii=False)
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

    # 1. 관리자 인계 트리거 키워드 검사
    direct_escalate_keywords = ["관리자", "sv", "담당자", "인계", "연결해"]
    is_direct_escalate = any(k in question.lower() for k in direct_escalate_keywords)

    correction_keywords = ["틀렸", "달라", "아닌", "다시", "확인해", "오답", "이상해"]
    is_correction = any(k in question.lower() for k in correction_keywords)

    # 2. 마스터 지식 검색
    evidence, has_match = search_memory_knowledge(question)

    # 3. 프롬프트 작성
    prompt = f"""당신은 고객센터 상담사를 실시간으로 돕는 전문 AI 어시스턴트입니다.
사내 공식 규정이 존재하는 경우 반드시 그 내용만을 기반으로 정확하게 답변하세요.

[상담사 질문]: {question}

[사내 공식 규정]:
{evidence if has_match else "사내 공식 규정 없음 (일반 상식/날씨 등 문의일 경우 일반적인 정확한 정보로 친절히 답변할 것)"}

[답변 서식 가이드]:
📢 *[핵심 답변]*: (결론 및 핵심 요약 2~3줄)
💡 *[상세 기준]*: (세부 진행 조건, 단계별 절차)
📌 *[상담 가이드 & 팁]*: (실무 팁 및 유의사항)
"""
    ai_reply = call_gemini_api(prompt)

    # 4. 인계 버튼 부착 여부 결정
    need_button = False
    if is_direct_escalate or is_correction or not has_match or not ai_reply:
        need_button = True

    if ai_reply and ai_reply.strip():
        final_text = ai_reply
    elif evidence:
        final_text = f"📢 *[핵심 답변]*:\n사내 마스터 규정 조회 결과입니다.\n\n{evidence}\n\n📌 *[상담 가이드 & 팁]*:\n- 공식 규정 원문을 기반으로 상담을 진행해 주세요."
    else:
        final_text = "📢 *[핵심 답변]*:\n사내 공식 지식에 등록되지 않은 항목입니다.\n\n📌 *[상담 가이드 & 팁]*:\n- 정확한 안내를 위해 아래 버튼을 눌러 SV(관리자) 확인을 진행해 주세요."

    # 5. 슬랙 전송
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
    content_type = request.headers.get("content-type", "")

    # A. 버튼 클릭 및 모달 팝업 상호작용 (application/x-www-form-urlencoded)
    if "application/x-www-form-urlencoded" in content_type:
        form_data = await request.form()
        payload = json.loads(form_data.get("payload", "{}"))
        p_type = payload.get("type")

        # 1) [🚨 관리자(SV) 확인 요청] 버튼 클릭 시 모달 열기
        if p_type == "block_actions":
            trigger_id = payload.get("trigger_id")
            action = payload.get("actions", [{}])[0]
            action_val = action.get("value", "{}")
            
            modal_view = {
                "type": "modal",
                "callback_id": "submit_escalate_modal",
                "private_metadata": action_val,
                "title": {"type": "plain_text", "text": "관리자(SV) 확인 요청"},
                "submit": {"type": "plain_text", "text": "제출하기"},
                "close": {"type": "plain_text", "text": "취소"},
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "⚠️ *AI 답변 내용에 이상이 있거나 추가 확인이 필요한가요?*\n상세 사유를 적어주시면 관리자에게 즉시 인계됩니다."}
                    },
                    {
                        "type": "input",
                        "block_id": "reason_block",
                        "label": {"type": "plain_text", "text": "확인 요청 사유"},
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "reason_input",
                            "multiline": True,
                            "placeholder": {"type": "plain_text", "text": "예: 지식 시트 기준과 금액이 다름 / 예외 케이스 처리 방법 확인 필요 등"}
                        }
                    }
                ]
            }
            try:
                slack_client.views_open(trigger_id=trigger_id, view=modal_view)
            except Exception as e:
                print(f"모달 팝업 실패: {e}")
            return {"status": "ok"}

        # 2) 모달 제출 시 스레드에 알림 게시
        elif p_type == "view_submission":
            user_name = payload.get("user", {}).get("name", "상담사")
            meta = json.loads(payload.get("view", {}).get("private_metadata", "{}"))
            channel_id = meta.get("c")
            thread_ts = meta.get("t")
            orig_q = meta.get("q", "")
            reason = payload.get("view", {}).get("state", {}).get("values", {}).get("reason_block", {}).get("reason_input", {}).get("value", "")

            notice_text = (
                f"🚨 *[관리자(SV) 확인 요청 접수됨]*\n"
                f"• *요청자*: @{user_name}\n"
                f"• *질의 요약*: {orig_q}\n"
                f"• *요청 사유*: {reason}\n\n"
                f"👉 _관리자(SV) 확인 후 스레드로 안내드릴 예정입니다._"
            )
            try:
                slack_client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=notice_text, mrkdwn=True)
            except Exception as e:
                print(f"인계 알림 전송 실패: {e}")
            return {"response_action": "clear"}

    # B. 일반 슬랙 메시지 이벤트 (application/json)
    data = await request.json()
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    event = data.get("event", {})
    if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        channel_id = event.get("channel")
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts")

        # 1) 메인 채널 신규 질문
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
        # 2) 스레드 내 후속 질문 / 오답 정정 감지
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
