import os
import re
import csv
import io
import json
import time
import urllib.parse
from collections import OrderedDict
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient
from dotenv import load_dotenv
from google import genai
from google.genai import types

# 1. .env 환경변수 로드
load_dotenv()

app = FastAPI()

# 2. Slack 클라이언트 및 API 키 로드
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

GEMINI_API_KEYS = []
for k, v in os.environ.items():
    if k.startswith("GEMINI_API_KEY") and v.strip():
        clean_key = v.strip().strip('"').strip("'")
        GEMINI_API_KEYS.append(clean_key)

api_key_index = 0
print(f"🔑 로드된 Gemini API 키 개수: {len(GEMINI_API_KEYS)}개")

# 3. 구글 스프레드시트 마스터 지식 베이스
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID", "1k-eGv2EFu4GG3H3oKG-Ql9IF-UVD_kZMTjnUiyLqLp4")
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{MASTER_SHEET_ID}/export?format=csv"

KNOWLEDGE_CACHE = []
LAST_SYNC_TIME = 0

def fetch_knowledge_base():
    """구글 시트 CSV를 파싱하여 전역 캐시에 적재"""
    global KNOWLEDGE_CACHE, LAST_SYNC_TIME
    try:
        res = requests.get(SHEET_CSV_URL, timeout=10)
        res.raise_for_status()
        res.encoding = 'utf-8'
        
        csv_reader = csv.reader(io.StringIO(res.text))
        rows = list(csv_reader)
        if not rows:
            return

        header = rows[0]
        parsed_data = []
        for row in rows[1:]:
            if not any(row):
                continue
            entry = {header[i].strip(): row[i].strip() for i in range(min(len(header), len(row)))}
            parsed_data.append(entry)
            
        KNOWLEDGE_CACHE = parsed_data
        LAST_SYNC_TIME = time.time()
        print(f"✅ 마스터 지식 로드 성공: 총 {len(KNOWLEDGE_CACHE)}건 캐싱 완료")
    except Exception as e:
        print(f"❌ 마스터 지식 로드 실패: {e}")

fetch_knowledge_base()

def normalize_text(text: str) -> str:
    """특수문자 및 공백 제거로 색인율 향상"""
    text = text.lower()
    return re.sub(r'[\s\-_/&]+', '', text)

def search_relevant_knowledge(query: str, top_k: int = 5) -> list:
    """사용자 질의 기반 연관 지식 검색"""
    global KNOWLEDGE_CACHE, LAST_SYNC_TIME
    if time.time() - LAST_SYNC_TIME > 3600:
        fetch_knowledge_base()
        
    cleaned_query = normalize_text(query)
    tokens = [t for t in re.split(r'\s+', query) if len(t) >= 2]
    
    scored_results = []
    for item in KNOWLEDGE_CACHE:
        score = 0
        item_str = " ".join(item.values())
        norm_item_str = normalize_text(item_str)
        
        if cleaned_query in norm_item_str:
            score += 10
        for token in tokens:
            if normalize_text(token) in norm_item_str:
                score += 3
        if score > 0:
            scored_results.append((score, item))
            
    scored_results.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored_results[:top_k]]

# 4. v1 API 엔드포인트 전용 호출 함수 (v1beta 404 방지)
def call_gemini_sdk(prompt: str) -> str:
    global api_key_index
    if not GEMINI_API_KEYS:
        return "⚠️ 등록된 Gemini API 키가 없습니다. .env 파일을 확인해 주세요."

    max_attempts = len(GEMINI_API_KEYS)
    last_error_msg = ""

    # REST v1 엔드포인트 직접 호출 (v1beta 404 우회)
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048
        }
    }

    # 계정에 따라 호환되는 모델 목록
    candidate_urls = [
        "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash-latest:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    ]

    for _ in range(max_attempts):
        current_key = GEMINI_API_KEYS[api_key_index]

        for base_url in candidate_urls:
            try:
                res = requests.post(
                    f"{base_url}?key={current_key}",
                    headers=headers,
                    json=payload,
                    timeout=25
                )
                if res.status_code == 200:
                    data = res.json()
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    last_error_msg = f"{res.status_code} {res.text}"
            except Exception as e:
                last_error_msg = str(e)

        api_key_index = (api_key_index + 1) % len(GEMINI_API_KEYS)

    return f"죄송합니다. AI 서비스 연결에 일시적인 지연이 발생했습니다.\n(상세 원인: {last_error_msg[:120]})"

# 5. Slack 비동기 질문 처리 및 이모지 상태 관리
def process_slack_question(channel: str, ts: str, user_text: str):
    # 1) 수신 확인: 질문 원본 메시지에 눈알(👀) 이모지 추가
    if slack_client:
        try:
            slack_client.reactions_add(
                channel=channel,
                timestamp=ts,
                name="eyes"
            )
        except Exception as e:
            print(f"⚠️ 👀 이모지 추가 실패: {e}")

    clean_prompt = re.sub(r'^\[질의-AI\]\s*', '', user_text).strip()
    matched_docs = search_relevant_knowledge(clean_prompt, top_k=4)
    context_str = json.dumps(matched_docs, ensure_ascii=False, indent=2) if matched_docs else "일치하는 규정 데이터 없음"

    system_instruction = f"""
당신은 상담원들을 지원하는 따뜻하고 상냥한 사내 AI 어시스턴트입니다. 
제공된 사내 규정 지식을 토대로 상담원들이 바로 고객 응대에 활용할 수 있게 친절하고 명확하게 안내해 주세요.

[사내 규정 지식 베이스]:
{context_str}

[사용자 질문]:
{clean_prompt}

[답변 작성 가이드라인]:
1. 첫 문장은 상담원에게 힘을 주는 따뜻하고 밝은 인사로 시작해 주세요. (예: "안녕하세요, 상담원님! 문의주신 내용 안내해 드릴게요. 😊")
2. 규정 내용은 핵심 항목별로 이모티콘(📌, ✔️, 💡 등)과 볼드체(*텍스트*)를 사용하여 가독성 높게 정리하세요.
3. 지식 베이스에 없는 내용일 경우 지어내지 말고, "해당 내용은 현재 규정집에 기재되어 있지 않아 담당 부서 확인이 필요합니다."라고 솔직하게 안내하세요.
4. 마무리는 "오늘도 힘찬 하루 보내세요! 추가로 궁금한 점이 있으시면 언제든 편하게 물어보세요. ✨" 같은 다정한 응원으로 끝맺어 주세요.
"""

    answer = call_gemini_sdk(system_instruction)

    if slack_client:
        # 2) 답변 완료: 기존 눈알(👀) 이모지 제거
        try:
            slack_client.reactions_remove(
                channel=channel,
                timestamp=ts,
                name="eyes"
            )
        except Exception as e:
            print(f"⚠️ 👀 이모지 삭제 실패: {e}")

        # 3) 스레드에 최종 답변 게시
        try:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=answer
            )
            # 4) 완료 체크(✅) 이모지 등록
            slack_client.reactions_add(
                channel=channel,
                timestamp=ts,
                name="white_check_mark"
            )
        except Exception as e:
            print(f"❌ 슬랙 메시지 발송 실패: {e}")

# 6. Slack Events 엔드포인트
PROCESSED_EVENT_IDS = OrderedDict()

@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    data = json.loads(body.decode("utf-8"))

    if "challenge" in data:
        return {"challenge": data["challenge"]}

    event_id = data.get("event_id")
    if event_id:
        if event_id in PROCESSED_EVENT_IDS:
            return {"status": "already_processed"}
        PROCESSED_EVENT_IDS[event_id] = True
        if len(PROCESSED_EVENT_IDS) > 1000:
            PROCESSED_EVENT_IDS.popitem(last=False)

    event = data.get("event", {})
    text = event.get("text", "")
    channel = event.get("channel")
    ts = event.get("ts")
    bot_id = event.get("bot_id")

    if not bot_id and text.startswith("[질의-AI]"):
        background_tasks.add_task(process_slack_question, channel, ts, text)

    return {"status": "ok"}
