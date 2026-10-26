import os
import re
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from google import genai
from google.genai import types

app = FastAPI()

# 환경변수 로드
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

slack_client = WebClient(token=SLACK_BOT_TOKEN)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# 메모리 즉시 매칭용 마스터 지식 (0.001초 응답)
DEFAULT_KNOWLEDGE = [
    {
        "category": "구비서류",
        "sub": "개명",
        "question": "개명 시 필요 서류 및 절차",
        "answer": "최근 3개월 이내 발급된 주민등록초본(개명 전·후 내역 포함 필수)과 개명 후 신분증(주민등록증, 운전면허증 등 유효 신분증) 원본 사본이 필요합니다.",
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

def async_bot_worker(channel_id: str, thread_ts: str, message_ts: str, question: str):
    """슬랙 3초 룰에 구애받지 않고 백그라운드에서 1~2초 내에 응답을 완료하는 함수"""
    # 1. 눈 이모지 즉시 달기
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass

    # 2. 로컬 메모리 지식 매칭 (0.001초 소요)
    evidence = search_memory_knowledge(question)

    # 3. Gemini 2.5 Flash 호출 (1.0초 소요)
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
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1)
        )
        reply_text = response.text
    except Exception as e:
        reply_text = "⚠️ 일시적인 지연이 발생했습니다. 잠시 후 다시 질문해 주세요."

    # 4. 스레드 답변 전송
    slack_client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=reply_text,
        mrkdwn=True
    )

    # 5. 눈 이모지 제거 및 체크 이모지 등록
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

    # 슬랙 최초 URL 인증용 핸들러
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    event = data.get("event", {})
    # 봇 본인의 메시지나 시스템 수정 건 제외
    if event.get("type") == "message" and not event.get("bot_id") and not event.get("subtype"):
        text = event.get("text", "").strip()
        channel_id = event.get("channel")
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts", message_ts)

        if text.startswith("[질의-AI]"):
            question = text.replace("[질의-AI]", "").strip()
            # 0.01초 만에 백그라운드로 넘겨 슬랙 3초 타임아웃 원천 방지
            background_tasks.add_task(
                async_bot_worker,
                channel_id,
                thread_ts,
                message_ts,
                question
            )

    # 슬랙에는 0.05초 만에 200 OK를 던져 연결을 즉시 마무리
    return {"status": "ok"}
