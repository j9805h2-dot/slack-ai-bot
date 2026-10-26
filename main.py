import os
import time
from fastapi import FastAPI, Request, BackgroundTasks
from google import genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

app = FastAPI()

# 환경변수에서 Slack 토큰 및 Gemini API 키 불러오기
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
slack_client = WebClient(token=SLACK_BOT_TOKEN)

# Gemini API 키 복수 로드 (1, 2, 3번 키 지원)
api_keys = [
    os.environ.get("GEMINI_API_KEY_1"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3")
]
valid_keys = [k for k in api_keys if k]
print(f"🔑 로드된 Gemini API 키 개수 : {len(valid_keys)}개")

# 현재 사용할 모델명 설정 (RPM 제한이 여유로운 모델로 지정)
# 예: "gemini-3.5-flash-lite" 또는 "gemini-1.5-flash"
GEMINI_MODEL_NAME = "gemini-3.5-flash-lite"

def get_gemini_client(key_index=0):
    """유효한 Gemini 클라이언트를 반환합니다."""
    if not valid_keys:
        raise ValueError("사용 가능한 Gemini API 키가 없습니다.")
    current_key = valid_keys[key_index % len(valid_keys)]
    return genai.Client(api_key=current_key)

@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    
    # Slack URL Verification 처리
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}
    
    # 이벤트 파싱
    event = body.get("event", {})
    
    # 봇 자신의 메시지이거나 메시지가 아니면 무시
    if event.get("bot_id") or event.get("subtype") == "bot_message":
        return {"status": "ignored"}
    
    if event.get("type") == "app_mention" or event.get("type") == "message":
        channel_id = event.get("channel")
        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts") or message_ts
        user_text = event.get("text", "")
        
        # 1. 요청 접수 확인용 이모지 먼저 남기기
        try:
            slack_client.reactions_add(
                channel=channel_id,
                name="white_check_mark",
                timestamp=message_ts
            )
        except SlackApiError as e:
            print(f"Slack 이모지 추가 에러: {e}")
            
        # 2. 백그라운드 태스크로 AI 응답 처리 (429 에러 방지를 위한 딜레이 및 폴백 포함)
        background_tasks.add_task(process_ai_response, channel_id, thread_ts, user_text)
        
    return {"status": "ok"}

def process_ai_response(channel_id: str, thread_ts: str, user_text: str):
    """Gemini API를 호출하여 답변을 생성하고 슬랙에 전송합니다."""
    response_text = ""
    success = False
    
    # 여러 개의 API 키를 순회하며 429 에러 발생 시 다음 키로 재시도
    for i in range(len(valid_keys) if valid_keys else 1):
        try:
            client = get_gemini_client(i)
            
            # Gemini 모델 호출 (모델명: gemini-3.5-flash-lite)
            response = client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=user_text,
            )
            response_text = response.text
            success = True
            break
            
        except Exception as e:
            print(f"⚠️ API 키 #{i+1} 호출 실패 또는 429 에러 발생: {e}")
            time.sleep(2) # 잠시 대기 후 다음 키 시도
            continue
            
    # 응답 전송
    try:
        if success and response_text:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=response_text
            )
        else:
            # 모든 키가 실패했거나 429 제한에 걸린 경우
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="⚠️ [AI 응답 일시 중단]\n요청 폭주로 인한 일시적 통신 제한(429)이 발생했습니다. 잠시 후(3~5초 뒤) 다시 질문해 주세요!\n\n📋 [에러 코드]:\n`429_RESOURCE_EXHAUSTED`"
            )
    except SlackApiError as e:
        print(f"Slack 메시지 전송 에러: {e}")
