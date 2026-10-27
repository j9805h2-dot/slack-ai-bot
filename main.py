import os
import re
import csv
import io
import json
import time
from collections import OrderedDict
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from slack_sdk import WebClient
from dotenv import load_dotenv

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

# 3. 구글 스프레드시트 설정
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID", "1k-eGv2EFu4GG3H3oKG-Ql9IF-UVD_kZMTjnUiyLqLp4")
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{MASTER_SHEET_ID}/export?format=csv"
LOG_WEBHOOK_URL = os.environ.get("LOG_WEBHOOK_URL", "")

KNOWLEDGE_CACHE = []
CORRECTED_CACHE = []
LAST_SYNC_TIME = 0

def fetch_master_knowledge():
    """기본 마스터 지식 로드"""
    global KNOWLEDGE_CACHE
    try:
        res = requests.get(SHEET_CSV_URL, timeout=10)
        res.raise_for_status()
        res.encoding = 'utf-8'
        csv_reader = csv.reader(io.StringIO(res.text))
        rows = list(csv_reader)
        if not rows:
            return
        header = rows[0]
        parsed = []
        for row in rows[1:]:
            if not any(row):
                continue
            entry = {header[i].strip(): row[i].strip() for i in range(min(len(header), len(row)))}
            parsed.append(entry)
        KNOWLEDGE_CACHE = parsed
        print(f"✅ 마스터 지식 로드 완료: {len(KNOWLEDGE_CACHE)}건")
    except Exception as e:
        print(f"❌ 마스터 지식 로드 실패: {e}")

def fetch_corrected_knowledge():
    """💬실시간_질의로그 및 📋상담사 질의의 관리자 보정 데이터 로드"""
    global CORRECTED_CACHE
    if not LOG_WEBHOOK_URL:
        return
    try:
        res = requests.get(LOG_WEBHOOK_URL, timeout=10)
        if res.status_code == 200:
            data = res.json()
            CORRECTED_CACHE = data.get("data", [])
            print(f"✅ 관리자 보정 피드백 지식 로드 완료: {len(CORRECTED_CACHE)}건")
    except Exception as e:
        print(f"⚠️ 보정 지식 로드 실패: {e}")

def refresh_all_knowledge():
    global LAST_SYNC_TIME
    fetch_master_knowledge()
    fetch_corrected_knowledge()
    LAST_SYNC_TIME = time.time()

refresh_all_knowledge()

def normalize_text(text: str) -> str:
    text = text.lower()
    return re.sub(r'[\s\-_/&]+', '', text)

def search_relevant_knowledge(query: str, top_k: int = 4) -> dict:
    global KNOWLEDGE_CACHE, CORRECTED_CACHE, LAST_SYNC_TIME
    if time.time() - LAST_SYNC_TIME > 600: # 10분 주기 동기화
        refresh_all_knowledge()

    cleaned_query = normalize_text(query)
    tokens = [t for t in re.split(r'\s+', query) if len(t) >= 2]

    def score_items(items):
        results = []
        for item in items:
            score = 0
            item_str = " ".join([str(v) for v in item.values()])
            norm_str = normalize_text(item_str)
            if cleaned_query in norm_str:
                score += 10
            for t in tokens:
                if normalize_text(t) in norm_str:
                    score += 3
            if score > 0:
                results.append((score, item))
        results.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in results[:top_k]]

    return {
        "corrected_rules": score_items(CORRECTED_CACHE),
        "master_rules": score_items(KNOWLEDGE_CACHE)
    }

# 4. 볼드체 변환 유틸 (Gemini **text** -> Slack *text*)
def convert_to_slack_markdown(text: str) -> str:
    # **텍스트** 를 *텍스트* 로 치환
    return re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)

# 5. Gemini 호출 함수
def call_gemini_api(prompt: str) -> str:
    global api_key_index
    if not GEMINI_API_KEYS:
        return "⚠️ 등록된 Gemini API 키가 없습니다."

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048}
    }

    active_models = ["gemini-flash-latest", "gemini-3-flash-preview", "gemini-3.5-flash", "gemini-pro-latest"]
    max_attempts = len(GEMINI_API_KEYS)
    last_error_msg = ""

    for _ in range(max_attempts):
        current_key = GEMINI_API_KEYS[api_key_index]
        for model in active_models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={current_key}"
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=25)
                if res.status_code == 200:
                    data = res.json()
                    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    # 볼드체 서식 변환 후 반환
                    return convert_to_slack_markdown(raw_text)
                else:
                    last_error_msg = f"{model} ({res.status_code}): {res.text}"
            except Exception as e:
                last_error_msg = str(e)

        api_key_index = (api_key_index + 1) % len(GEMINI_API_KEYS)

    return f"죄송합니다. AI 서비스 연결에 일시적인 지연이 발생했습니다.\n(원인: {last_error_msg[:100]})"

# 6. 스레드 이전 대화 내역 조회 (연속 대화 맥락)
def get_thread_context(channel: str, thread_ts: str) -> tuple:
    if not slack_client or not thread_ts:
        return "", ""
    try:
        res = slack_client.conversations_replies(channel=channel, ts=thread_ts, limit=15)
        messages = res.get("messages", [])
        if not messages:
            return "", ""
            
        first_question = messages[0].get("text", "")
        history = []
        for m in messages[:-1]: # 현재 메시지 직전까지 수집
            role = "AI" if "bot_id" in m else "상담원"
            history.append(f"{role}: {m.get('text', '')}")
            
        return first_question, "\n".join(history)
    except Exception as e:
        print(f"⚠️ 스레드 문맥 조회 실패: {e}")
        return "", ""

# 7. 구글 시트 로그 전송
def log_to_google_sheet(gubun: str, thread_ts: str, user_id: str, first_q: str, sub_q: str, answer: str):
    if not LOG_WEBHOOK_URL:
        return
    try:
        requests.post(LOG_WEBHOOK_URL, json={
            "gubun": gubun,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "first_question": first_q,
            "sub_question": sub_q,
            "answer": answer
        }, timeout=5)
    except Exception as e:
        print(f"⚠️ 시트 로깅 통신 실패: {e}")

# 8. 질문 처리 및 응답 로직
def process_slack_question(channel: str, ts: str, thread_ts: str, user_text: str, user_id: str):
    is_thread_reply = bool(thread_ts and thread_ts != ts)
    target_ts = thread_ts if is_thread_reply else ts

    # 확인용 이모지 👀 추가
    if slack_client:
        try:
            slack_client.reactions_add(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass

    clean_prompt = re.sub(r'^\[질의-AI\]\s*', '', user_text).strip()
    
    # 스레드 대댓글인 경우 맥락 조회
    first_q_text, thread_history = ("", "")
    if is_thread_reply:
        first_q_text, thread_history = get_thread_context(channel, thread_ts)
        first_q_clean = re.sub(r'^\[질의-AI\]\s*', '', first_q_text).strip()
    else:
        first_q_clean = clean_prompt

    # 관련 규정 및 보정 지식 검색
    search_target = f"{first_q_clean} {clean_prompt}" if is_thread_reply else clean_prompt
    knowledge = search_relevant_knowledge(search_target)

    system_instruction = f"""
당신은 상담원들을 지원하는 따뜻하고 상냥한 사내 AI 어시스턴트입니다.

[⚠️ 최우선 검수 보정 지식 (관리자가 수정한 피드백)]:
{json.dumps(knowledge['corrected_rules'], ensure_ascii=False, indent=2)}
*중요 지침*: 위 최우선 보정 지식에 관리자가 두서없이 적어둔 메모나 보정 내용이 있더라도, 그 핵심 의도를 완벽히 파악하여 빠짐없이 반영하세요. 사용자가 보기에 다정하고 정돈된 최종 표준 가이드라인 양식으로 재구성해 답변해야 합니다.

[사내 규정 지식 베이스]:
{json.dumps(knowledge['master_rules'], ensure_ascii=False, indent=2)}

[이전 스레드 대화 맥락]:
{thread_history if thread_history else "신규 질문입니다."}

[상담원의 현재 질문]:
{clean_prompt}

[답변 작성 가이드라인]:
1. 첫 문장은 상담원에게 힘을 주는 따뜻하고 밝은 인사로 시작하세요.
2. 내용은 핵심 항목별로 이모티콘(📌, ✔️, 💡 등)과 볼드체를 적절히 사용하여 가독성 높게 정리하세요.
3. 스레드 연속 대화인 경우, 이전 답변과 자연스럽게 연결되는 어조로 답변하세요.
4. 마무리는 다정하고 힘찬 응원으로 끝맺어 주세요.
"""

    answer = call_gemini_api(system_instruction)

    if slack_client:
        # 👀 삭제
        try:
            slack_client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass

        # 스레드 답변 전송 및 ✅ 추가
        try:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=target_ts,
                text=answer
            )
            slack_client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
        except Exception as e:
            print(f"❌ 슬랙 전송 실패: {e}")

    # 구글 시트에 A~I열 규격으로 저장
    gubun_val = "대댓글질의" if is_thread_reply else "최초답변"
    sub_q_val = clean_prompt if is_thread_reply else ""
    log_to_google_sheet(
        gubun=gubun_val,
        thread_ts=target_ts,
        user_id=user_id,
        first_q=first_q_clean,
        sub_q=sub_q_val,
        answer=answer
    )

# 9. 슬랙 이벤트 라우팅
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
    thread_ts = event.get("thread_ts")
    bot_id = event.get("bot_id")
    user_id = event.get("user", "상담원")

    if not bot_id:
        # 최초 질문([질의-AI])이거나 생성된 스레드 안에서의 대댓글인 경우 자동 처리
        if text.startswith("[질의-AI]") or (thread_ts and thread_ts != ts):
            background_tasks.add_task(process_slack_question, channel, ts, thread_ts, text, user_id)

    return {"status": "ok"}
