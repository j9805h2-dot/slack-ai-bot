import os
import re
import csv
import io
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient
from google import genai
from google.genai import types

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

# 2. 구글 시트 마스터 지식 실시간 동기화 설정
COUNSELOR_SHEET_ID = "1s_K0W43kUhRctN_mqEoDA-yj9qRnPFiHucFIs1AoJhY"
MASTER_SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{COUNSELOR_SHEET_ID}/gviz/tq?tqx=out:csv&sheet=%F0%9F%93%9AAI_%EB%A7%88%EC%8A%A4%ED%84%B0%EC%A7%80%EC%8B%9D"

# 메모리 캐시 저장소
CACHED_KNOWLEDGE = []

def sync_master_knowledge_from_sheet():
    """구글 시트의 📚AI_마스터지식 탭 전체를 메모리로 0.5초 만에 동기화"""
    global CACHED_KNOWLEDGE
    try:
        res = requests.get(MASTER_SHEET_CSV_URL, timeout=8)
        if res.status_code == 200:
            f = io.StringIO(res.text)
            reader = csv.reader(f)
            rows = list(reader)
            
            new_data = []
            # 헤더 행 제외하고 2번째 행부터 적재 (대분류, 소분류, 질문, 답변, 키워드)
            for r in rows[1:]:
                if len(r) >= 4:
                    category = r[0].strip() if len(r) > 0 else ""
                    sub = r[1].strip() if len(r) > 1 else ""
                    q = r[2].strip() if len(r) > 2 else ""
                    a = r[3].strip() if len(r) > 3 else ""
                    kw = r[4].strip() if len(r) > 4 else ""
                    
                    if q and a:
                        keywords_list = [k.strip().lower() for k in kw.split(",") if k.strip()]
                        # 질문 텍스트 자체도 키워드로 포함
                        keywords_list.extend(re.findall(r"[\w가-힣]+", q.lower()))
                        new_data.append({
                            "category": category,
                            "sub": sub,
                            "question": q,
                            "answer": a,
                            "keywords": list(set(keywords_list))
                        })
            if new_data:
                CACHED_KNOWLEDGE = new_data
                print(f"✅ 구글 마스터 지식 동기화 성공: 총 {len(CACHED_KNOWLEDGE)}건 로드됨")
                return
    except Exception as e:
        print(f"⚠️ 시트 동기화 실패 (기본 캐시 유지): {e}")

# 서버 시작 시 즉시 시트 지식 1회 전수 로드
sync_master_knowledge_from_sheet()

def search_memory_knowledge(query: str) -> str:
    """구글 시트에서 가져온 수백 개 지식 중 질문과 가장 일치하는 규정 1~3건 추출"""
    clean_q = re.sub(r"[^\w\s가-힣]", " ", query.lower())
    q_tokens = [t for t in clean_q.split() if len(t) > 1]

    scored = []
    for item in CACHED_KNOWLEDGE:
        score = 0
        # 키워드 매칭 점수
        for token in q_tokens:
            for kw in item["keywords"]:
                if token in kw or kw in token:
                    score += 5
            if token in item["question"].lower():
                score += 10

        if score > 0:
            scored.append((score, f"[{item['sub']} - {item['question']}]\n{item['answer']}"))

    scored.sort(key=lambda x: x[0], reverse=True)
    return "\n\n---\n\n".join([item[1] for item in scored[:3]])

def call_gemini_api(prompt: str) -> str:
    models_to_try = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
    for api_key in GEMINI_API_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            for model_name in models_to_try:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.1,
                            max_output_tokens=2048
                        )
                    )
                    if response and response.text:
                        return response.text
                except Exception:
                    continue
        except Exception:
            continue
    return "⚠️ 일시적인 지연이 발생했습니다. 잠시 후 다시 질문해 주세요."

def async_bot_worker(channel_id: str, thread_ts: str, message_ts: str, question: str):
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass

    # 구글 시트 마스터 지식에서 정답 규정 탐색
    evidence = search_memory_knowledge(question)

    prompt = f"""당신은 고객센터 상담사를 실시간으로 돕는 전문 AI 어시스턴트입니다.
반드시 아래 [사내 공식 규정]에 적힌 내용만을 바탕으로 답변하세요. 지식 베이스에 없는 내용은 추측하지 마세요.

[상담사 질문]: {question}

[사내 공식 규정]:
{evidence if evidence else "관련 공식 규정이 지식 베이스에 등록되어 있지 않습니다."}

[답변 작성 가이드]:
📢 *[핵심 답변]*: (결론 및 필수 구비서류)
💡 *[상세 기준]*: (상세 내역 및 프로세스)
📌 *[상담 가이드 & 팁]*: (실무 유의사항)
(※ 규정에 없는 내용일 경우 '사내 공식 지식에 미등록된 항목이므로 SV/관리자 확인이 필요합니다'라고 명시할 것)"""

    reply_text = call_gemini_api(prompt)

    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=reply_text,
            mrkdwn=True
        )
    except Exception as e:
        print(f"메시지 전송 에러: {e}")

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
    return {
        "status": "ok",
        "service": "Slack AI Bot",
        "loaded_knowledge_count": len(CACHED_KNOWLEDGE)
    }

# 시트 내용 수정 시 바로 갱신할 수 있는 새로고침 엔드포인트
@app.get("/sync")
def manual_sync():
    sync_master_knowledge_from_sheet()
    return {"status": "synced", "count": len(CACHED_KNOWLEDGE)}

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
