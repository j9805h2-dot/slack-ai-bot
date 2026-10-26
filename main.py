import os
import re
import csv
import io
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient

app = FastAPI()

# 1. 환경변수
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
API_KEYS = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
]
GEMINI_API_KEYS = [k.strip() for k in API_KEYS if k]

slack_client = WebClient(token=SLACK_BOT_TOKEN)

# 2. 마스터 지식 시트 캐싱
MASTER_SHEET_ID = "1k-eGv2EFu4GG3H3oKG-Ql9IF-UVD_kZMTjnUiyLqLp4"
CSV_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{MASTER_SHEET_ID}/gviz/tq?tqx=out:csv&sheet=%F0%9F%93%9AAI_%EB%A7%88%EC%8A%A4%ED%84%B0%EC%A7%80%EC%8B%9D"

CACHED_KNOWLEDGE = []

def sync_master_knowledge():
    global CACHED_KNOWLEDGE
    try:
        res = requests.get(CSV_EXPORT_URL, timeout=8)
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

def search_memory_knowledge(query: str) -> str:
    clean_q = re.sub(r"[^\w\s가-힣]", " ", query.lower())
    tokens = [t for t in clean_q.split() if len(t) > 1]

    scored = []
    for item in CACHED_KNOWLEDGE:
        score = 0
        for token in tokens:
            for kw in item["keywords"]:
                if token in kw or kw in token:
                    score += 5
            if token in item["question"].lower():
                score += 15

        if score > 0:
            scored.append((score, f"[{item['category']} > {item['sub']} - {item['question']}]\n{item['answer']}"))

    scored.sort(key=lambda x: x[0], reverse=True)
    return "\n\n---\n\n".join([item[1] for item in scored[:3]])

def call_gemini_direct_rest(prompt: str) -> str:
    # 503 트래픽 과부하를 회피하기 위해 다중 모델 순환
    models = [
        "gemini-2.0-flash",
        "gemini-2.5-flash",
        "gemini-flash-latest"
    ]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048
        }
    }

    last_err = ""
    for model in models:
        for key in GEMINI_API_KEYS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            try:
                res = requests.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=5
                )
                if res.status_code == 200:
                    data = res.json()
                    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    if parts and "text" in parts[0]:
                        return parts[0]["text"]
                elif res.status_code == 503:
                    last_err = f"503 모델 과부하({model})"
                    continue
                else:
                    last_err = f"API 오류 {res.status_code}: {res.text[:100]}"
            except Exception as e:
                last_err = f"호출 예외: {str(e)}"
                continue

    return f"⚠️ 지식 조회 오류: {last_err}"

def async_bot_worker(channel_id: str, thread_ts: str, message_ts: str, question: str):
    # 1. 눈 이모지
    try:
        slack_client.reactions_add(channel=channel_id, timestamp=message_ts, name="eyes")
    except Exception:
        pass

    # 2. 1,347건 마스터 지식 검색
    evidence = search_memory_knowledge(question)

    # 3. Gemini 프롬프트 구성 및 REST 호출
    prompt = f"""당신은 고객센터 상담사를 실시간으로 돕는 전문 AI 어시스턴트입니다.
반드시 아래 [사내 공식 규정]에 명시된 내용만을 기반으로 정확하게 작성하세요. 지식에 없는 내용은 절대 지어내지 마세요.

[상담사 질문]: {question}

[사내 공식 규정]:
{evidence if evidence else "관련 공식 규정이 지식 베이스에 등록되어 있지 않습니다."}

[답변 가이드]:
📢 *[핵심 답변]*: (결론 및 필수 구비서류)
💡 *[상세 기준]*: (상세 내역 및 프로세스)
📌 *[상담 가이드 & 팁]*: (실무 유의사항)
(※ 규정에 없는 내용일 경우 '사내 공식 지식에 미등록된 항목이므로 SV 확인이 필요합니다' 명시)"""

    # 함수명 오타 수정 완료
    reply_text = call_gemini_direct_rest(prompt)

    # 4. 스레드 전송
    try:
        slack_client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=reply_text,
            mrkdwn=True
        )
    except Exception as e:
        print(f"❌ 슬랙 전송 실패: {e}")

    # 5. 이모지 교체 (eyes -> white_check_mark)
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
        "loaded_knowledge_count": len(CACHED_KNOWLEDGE)
    }

@app.get("/sync")
def manual_sync():
    count = sync_master_knowledge()
    return {"status": "synced", "count": count}

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
