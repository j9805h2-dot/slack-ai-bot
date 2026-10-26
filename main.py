import os
import re
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient

app = FastAPI()

# 1. 환경변수 로드
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
API_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
]
GEMINI_API_KEYS = [k.strip() for k in API_KEYS if k]

slack_client = WebClient(token=SLACK_BOT_TOKEN)

# 2. 로컬 메모리 마스터 지식 베이스
DEFAULT_KNOWLEDGE = [
    {
        "category": "구비서류",
        "sub": "개명",
        "question": "개명 시 필요 서류 및 절차",
        "answer": "최근 3개월 이내 발급된 주민등록초본(개명 전·후 내역 포함 필수)과 개명 후 신분증(주민등록증, 운전면허증 등 유효 신분증) 사본이 필요합니다.",
        "keywords": ["개명", "서류", "초본", "신분증"]
    },
    {
        "category": "구비서류",
        "sub": "대리납부",
        "question": "대리납부 구비서류 기준",
        "answer": "초회 납부는 [신분증 2개], 초회 이후 납부는 [신분증 2개 + 3개월 내 가족관계증명서 + 통장사본]이 필수입니다.",
        "keywords": ["대리납부", "대납", "가족관계증명서", "직계"]
    },
    {
        "category": "구비서류",
        "sub": "외국인",
        "question": "외국인 실명인증 서류",
        "answer": "외국인등록증 앞·뒷면 사본(체류기간 명시 필수)이 필요합니다.",
        "keywords": ["외국인", "외국인등록증", "체류기간"]
    },
    {
        "category": "상품규정",
        "sub": "비앤케어팩 캐시백",
        "question": "비앤케어팩 캐시백 입금일 및 세부 일정",
        "answer": "정규 캐시백 입금일은 매월 28일입니다.\n- 2~37: 1회 0원 / 2~11회 월 47,500원 / 12~37회 월 37,500원 (총 145만)\n- 4~48: 1~3회 0원 / 4~47회 월 32,230원 / 48회 31,880원 (총 145만)\n- 4~60: 1~3회 0원 / 4~59회 월 25,440원 / 60회 25,360원 (총 145만)\n- 4~72: 1~3회 0원 / 4~71회 월 21,020원 / 72회 20,640원 (총 145만)",
        "keywords": ["캐시백", "캐쉬백", "비앤케어팩", "입금일", "28일"]
    }
]

def search_memory_knowledge(query: str) -> str:
    clean_q = re.sub(r"[^\w\s가-힣]", " ", query.lower())
    matched = []
    for item in DEFAULT_KNOWLEDGE:
        score = sum(3 for kw in item["keywords"] if kw in clean_q)
        if "개명" in clean_q and "개명" in item["keywords"]: score += 10
        if "대납" in clean_q and "대리납부" in item["keywords"]: score += 10
        if "캐시백" in clean_q and "캐시백" in item["keywords"]: score += 10

        if score > 0:
            matched.append((score, f"[{item['sub']} - {item['question']}]\n{item['answer']}"))

    matched.sort(key=lambda x: x[0], reverse=True)
    return "\n\n---\n\n".join([m[1] for m in matched[:3]])

def call_gemini_api_direct(prompt: str) -> str:
    """확인된 Gemini 2.5 REST API 직접 호출 (인증 거절 원천 방지)"""
    models = ["gemini-2.5-flash", "gemini-flash-latest"]
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048
        }
    }

    for key in GEMINI_API_KEYS:
        for model in models:
            # v1beta 및 v1 양방향 시도
            for ver in ["v1beta", "v1"]:
                url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={key}"
                try:
                    res = requests.post(
                        url,
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json=payload,
                        timeout=10
                    )
                    if res.status_code == 200:
                        data = res.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts and "text" in parts[0]:
                                return parts[0]["text"]
                except Exception as e:
                    print(f"API 호출 예외: {e}")
                    continue

    return "⚠️ 일시적인 지연이 발생했습니다. 잠시 후 다시 질문해 주세요."

def async_bot_worker(channel_id: str, thread_ts: str, message_ts: str, question: str):
    # 1. 눈 이모지 등록
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass

    # 2. 로컬 메모리 지식 매칭
    evidence = search_memory_knowledge(question)

    # 3. Gemini 호출
    prompt = f"""
당신은 고객센터 상담사를 실시간으로 돕는 전문 AI 어시스턴트입니다.
사내 공식 규정을 바탕으로 신속하고 정확한 실무 답변을 작성하세요.

[상담사 질문]: {question}

[사내 지식 베이스]:
{evidence if evidence else "관련 기본 규정 준수"}

[답변 작성 가이드]:
📢 *[핵심 답변]*: (결론 및 필수 구비서류)
💡 *[상세 기준]*: (상세 내역 및 프로세스)
📌 *[상담 가이드 & 팁]*: (실무 유의사항)
"""
    reply_text = call_gemini_api_direct(prompt)

    # 4. 슬랙 스레드 전송
    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=reply_text,
            mrkdwn=True
        )
    except Exception as e:
        print(f"메시지 전송 실패: {e}")

    # 5. 이모지 교체 (eyes -> white_check_mark)
    try:
        slack_client.reactions_remove(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="white_check_mark")
    except Exception:
        pass

@app.post("/slack/events")
async def handle_slack_events(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()

    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    event = data.get("event", {})
    if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        channel_id = event.get("channel")
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts", message_ts)

        if text.startswith("[질의-AI]"):
            question = text.replace("[질의-AI]", "").strip()
            background_tasks.add_task(
                async_bot_worker,
                channel_id,
                thread_ts,
                message_ts,
                question
            )

    return {"status": "ok"}
