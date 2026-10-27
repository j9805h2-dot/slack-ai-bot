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

# .env 환경변수 파일 자동 로드
load_dotenv()

app = FastAPI()

# 1. 환경변수 로드 및 API 키 수집
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

# 다중 Gemini API 키 로드 (GEMINI_API_KEY_1, GEMINI_API_KEY_2 등 자동 탐색)
GEMINI_API_KEYS = []
for k, v in os.environ.items():
    if k.startswith("GEMINI_API_KEY") and v.strip():
        GEMINI_API_KEYS.append(v.strip())

api_key_index = 0

print(f"🔑 로드된 Gemini API 키 개수: {len(GEMINI_API_KEYS)}개")

# 2. 구글 스프레드시트 마스터 지식 베이스
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
            print("⚠️ 구글 시트 데이터가 비어 있습니다.")
            return

        header = rows[0]
        parsed_data = []
        for row in rows[1:]:
            if not any(row):
                continue
            entry = {}
            for idx, col_name in enumerate(header):
                val = row[idx].strip() if idx < len(row) else ""
                entry[col_name.strip()] = val
            parsed_data.append(entry)
            
        KNOWLEDGE_CACHE = parsed_data
        LAST_SYNC_TIME = time.time()
        print(f"✅ 마스터 지식 로드 성공: 총 {len(KNOWLEDGE_CACHE)}건 캐싱 완료")
    except Exception as e:
        print(f"❌ 마스터 지식 로드 실패: {e}")

# 서버 시작 시 1회 지식 적재
fetch_knowledge_base()

# 3. 텍스트 정규화 및 키워드 검색 함수
def normalize_text(text: str) -> str:
    """특수문자 및 공백을 정규화하여 색인율을 높임"""
    text = text.lower()
    text = re.sub(r'[\s\-_/&]+', '', text)
    return text

def search_relevant_knowledge(query: str, top_k: int = 5) -> list:
    """사용자 질의와 연관된 지식을 우선순위로 검색"""
    global KNOWLEDGE_CACHE, LAST_SYNC_TIME
    # 캐시 유효기간(1시간) 만료 시 갱신
    if time.time() - LAST_SYNC_TIME > 3600:
        fetch_knowledge_base()
        
    cleaned_query = normalize_text(query)
    tokens = [t for t in re.split(r'\s+', query) if len(t) >= 2]
    
    scored_results = []
    for item in KNOWLEDGE_CACHE:
        score = 0
        item_str = " ".join(item.values())
        norm_item_str = normalize_text(item_str)
        
        # 완전 일치 가산점
        if cleaned_query in norm_item_str:
            score += 10
            
        # 개별 토큰 일치 가산점
        for token in tokens:
            if normalize_text(token) in norm_item_str:
                score += 3
                
        if score > 0:
            scored_results.append((score, item))
            
    # 점수 높은 순 정렬
    scored_results.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored_results[:top_k]]

# 4. Gemini REST API 순환 호출 함수
def call_gemini_api_rest(prompt: str) -> str:
    global api_key_index
    if not GEMINI_API_KEYS:
        return "⚠️ 등록된 Gemini API 키가 없습니다. .env 파일을 확인해 주세요."

    # Gemini 2.5 Flash 엔드포인트
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
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

    max_attempts = len(GEMINI_API_KEYS)
    for _ in range(max_attempts):
        current_key = GEMINI_API_KEYS[api_key_index]
        req_url = f"{url}?key={current_key}"
        
        try:
            res = requests.post(req_url, headers=headers, json=payload, timeout=25)
            if res.status_code == 200:
                data = res.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            else:
                print(f"⚠️ 키 호출 오류 ({res.status_code}): 다음 키로 전환합니다.")
        except Exception as e:
            print(f"⚠️ 키 호출 실패 ({e}): 다음 키로 전환합니다.")

        # 키 순환
        api_key_index = (api_key_index + 1) % len(GEMINI_API_KEYS)

    return "죄송합니다. AI 서비스 연결에 일시적인 지연이 발생했습니다. 잠시 후 다시 시도해 주세요."

# 5. 비동기 질문 처리 및 Slack 메시지 전송
def process_slack_question(channel: str, ts: str, user_text: str):
    # 접두어 제거
    clean_prompt = re.sub(r'^\[질의-AI\]\s*', '', user_text).strip()
    
    # 지식 베이스 검색
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
2. 규정 내용은 핵심 항목별로 이모티콘(📌, ✔️, 💡 등)과 볼드체(`*텍스트*`)를 사용하여 가독성 높게 정리하세요.
3. 지식 베이스에 없는 내용일 경우 지어내지 말고, "해당 내용은 현재 규정집에 기재되어 있지 않아 담당 부서 확인이 필요합니다."라고 솔직하게 안내하세요.
4. 마무리는 "오늘도 힘찬 하루 보내세요! 추가로 궁금한 점이 있으시면 언제든 편하게 물어보세요. ✨" 같은 다정한 응원으로 끝맺어 주세요.
"""

    answer = call_gemini_api_rest(system_instruction)

    if slack_client:
        try:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=answer
            )
        except Exception as e:
            print(f"❌ 슬랙 메시지 발송 실패: {e}")

# 6. Slack Events 수신 엔드포인트
PROCESSED_EVENT_IDS = OrderedDict()

@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    data = json.loads(body.decode("utf-8"))

    # 1) Slack URL 검증 (Challenge)
    if "challenge" in data:
        return {"challenge": data["challenge"]}

    # 2) 이벤트 중복 수신 방지
    event_id = data.get("event_id")
    if event_id:
        if event_id in PROCESSED_EVENT_IDS:
            return {"status": "already_processed"}
        PROCESSED_EVENT_IDS[event_id] = True
        if len(PROCESSED_EVENT_IDS) > 1000:
            PROCESSED_EVENT_IDS.popitem(last=False)

    # 3) 메시지 처리
    event = data.get("event", {})
    text = event.get("text", "")
    channel = event.get("channel")
    ts = event.get("ts")
    bot_id = event.get("bot_id")

    # 봇 본인이 쓴 글 제외 및 [질의-AI] 포함 여부 확인
    if not bot_id and text.startswith("[질의-AI]"):
        background_tasks.add_task(process_slack_question, channel, ts, text)

    return {"status": "ok"}
