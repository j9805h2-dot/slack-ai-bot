import os
import re
import csv
import io
import json
import time
from collections import OrderedDict
import requests
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, Response
from slack_sdk import WebClient
from dotenv import load_dotenv

# 1. 환경변수 로드
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

# 3. 구글 스프레드시트 지식 베이스
MASTER_SHEET_ID = os.environ.get("MASTER_SHEET_ID", "1k-eGv2EFu4GG3H3oKG-Ql9IF-UVD_kZMTjnUiyLqLp4")
SHEET_CSV_URL = f"https://docs.google.com/spreadsheets/d/{MASTER_SHEET_ID}/export?format=csv"
LOG_WEBHOOK_URL = os.environ.get("LOG_WEBHOOK_URL", "")

KNOWLEDGE_CACHE = []
CORRECTED_CACHE = []
LAST_SYNC_TIME = 0

def fetch_master_knowledge():
    global KNOWLEDGE_CACHE
    try:
        res = requests.get(SHEET_CSV_URL, timeout=10)
        res.raise_for_status()
        res.encoding = 'utf-8'
        rows = list(csv.reader(io.StringIO(res.text)))
        if not rows:
            return
        header = rows[0]
        KNOWLEDGE_CACHE = [
            {header[i].strip(): row[i].strip() for i in range(min(len(header), len(row)))}
            for row in rows[1:] if any(row)
        ]
        print(f"✅ 마스터 지식 로드 완료: {len(KNOWLEDGE_CACHE)}건")
    except Exception as e:
        print(f"❌ 마스터 지식 로드 실패: {e}")

def fetch_corrected_knowledge():
    global CORRECTED_CACHE
    if not LOG_WEBHOOK_URL:
        return
    try:
        res = requests.get(LOG_WEBHOOK_URL, timeout=10)
        if res.status_code == 200:
            data = res.json()
            CORRECTED_CACHE = data.get("data", [])
            print(f"✅ 실시간 보정 지식 로드 완료: {len(CORRECTED_CACHE)}건")
    except Exception as e:
        print(f"⚠️ 보정 지식 로드 실패: {e}")

def refresh_all_knowledge():
    global LAST_SYNC_TIME
    fetch_master_knowledge()
    fetch_corrected_knowledge()
    LAST_SYNC_TIME = time.time()

refresh_all_knowledge()

def normalize_text(text: str) -> str:
    return re.sub(r'[\s\-_/&]+', '', text.lower())

def search_relevant_knowledge(query: str, top_k: int = 4) -> dict:
    global KNOWLEDGE_CACHE, CORRECTED_CACHE, LAST_SYNC_TIME
    if time.time() - LAST_SYNC_TIME > 600:
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
    return re.sub(r'\*\*(.*?)\*\*', r'*\1*', text)

# 5. Gemini API 호출 함수
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
                    return convert_to_slack_markdown(raw_text)
                else:
                    last_error_msg = f"{model} ({res.status_code})"
            except Exception as e:
                last_error_msg = str(e)

        api_key_index = (api_key_index + 1) % len(GEMINI_API_KEYS)

    return f"죄송합니다. AI 서비스 연결에 일시적인 지연이 발생했습니다.\n(원인: {last_error_msg[:100]})"

# 6. 스레드 히스토리 및 질문 횟수 카운트
def get_thread_context_and_count(channel: str, thread_ts: str) -> tuple:
    if not slack_client or not thread_ts:
        return "", "", 1
    try:
        res = slack_client.conversations_replies(channel=channel, ts=thread_ts, limit=20)
        messages = res.get("messages", [])
        if not messages:
            return "", "", 1

        first_q = messages[0].get("text", "")
        history = []
        user_question_count = 0

        for m in messages[:-1]:
            is_bot = "bot_id" in m
            if not is_bot:
                user_question_count += 1
            role = "AI" if is_bot else "상담원"
            history.append(f"{role}: {m.get('text', '')}")

        total_user_count = user_question_count + 1
        return first_q, "\n".join(history), total_user_count
    except Exception as e:
        print(f"⚠️ 스레드 문맥 조회 예외: {e}")
        return "", "", 1

# 7. 관리자/팀장/SV 질의 의도 감지
def check_manual_escalation_intent(text: str) -> bool:
    keywords = ["팀장", "관리자", "sv", "에스브이", "상급자", "물어볼", "물어봐야", "확인해볼", "올려볼", "질의서"]
    normalized = text.lower()
    return any(k in normalized for k in keywords)

# 8. 질의로그 시트 전송
def log_to_google_sheet(gubun: str, thread_ts: str, user_id: str, first_q: str, sub_q: str, answer: str):
    if not LOG_WEBHOOK_URL:
        return
    try:
        requests.post(LOG_WEBHOOK_URL, json={
            "action": "log",
            "gubun": gubun,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "first_question": first_q,
            "sub_question": sub_q,
            "answer": answer
        }, timeout=5)
    except Exception as e:
        print(f"⚠️ 질의로그 시트 전송 실패: {e}")

# 9. 캡처 이미지와 100% 동일한 모달 UI 뷰 생성
def build_modal_view_dict(channel_id: str, thread_ts: str, user_name: str, default_q: str = "") -> dict:
    return {
        "type": "modal",
        "callback_id": "submit_inquiry_modal",
        "title": {"type": "plain_text", "text": "📋 관리자(SV) 질의 접수"},
        "submit": {"type": "plain_text", "text": "전송"},
        "close": {"type": "plain_text", "text": "취소"},
        "private_metadata": json.dumps({
            "channel_id": channel_id,
            "thread_ts": thread_ts
        }),
        "blocks": [
            {
                "type": "input",
                "block_id": "team_block",
                "label": {"type": "plain_text", "text": "소속팀 *"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "team_input",
                    "placeholder": {"type": "plain_text", "text": "예: 1팀, 6팀"}
                }
            },
            {
                "type": "input",
                "block_id": "author_block",
                "label": {"type": "plain_text", "text": "상담사명(작성자) *"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "author_input",
                    "initial_value": user_name if user_name else "",
                    "placeholder": {"type": "plain_text", "text": "성함을 입력하세요."}
                }
            },
            {
                "type": "input",
                "block_id": "contract_block",
                "label": {"type": "plain_text", "text": "계약번호 / 연락처 *"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "contract_input",
                    "placeholder": {"type": "plain_text", "text": "계약번호 또는 전화번호"}
                }
            },
            {
                "type": "input",
                "block_id": "name_block",
                "label": {"type": "plain_text", "text": "회원명 *"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "name_input",
                    "placeholder": {"type": "plain_text", "text": "회원명을 입력하세요."}
                }
            },
            {
                "type": "input",
                "block_id": "customerQ_block",
                "label": {"type": "plain_text", "text": "고객 문의 내용(고객) *"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "customerQ_input",
                    "placeholder": {"type": "plain_text", "text": "고객이 인입 시 문의한 핵심 내용"}
                }
            },
            {
                "type": "input",
                "block_id": "myQ_block",
                "label": {"type": "plain_text", "text": "문의 내용(내가 궁금한 점) *"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "myQ_input",
                    "multiline": True,
                    "initial_value": default_q if default_q else "",
                    "placeholder": {"type": "plain_text", "text": "확인받고자 하는 구체적인 내용 작성"}
                }
            }
        ]
    }

# 10. 질문 처리 및 응답 전송 로직
def process_slack_question(channel: str, ts: str, thread_ts: str, user_text: str, user_id: str):
    is_thread_reply = bool(thread_ts and thread_ts != ts)
    target_ts = thread_ts if is_thread_reply else ts

    if slack_client:
        try:
            slack_client.reactions_add(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass

    clean_prompt = re.sub(r'^\[질의-AI\]\s*', '', user_text).strip()

    first_q_text, thread_history, user_q_count = ("", "", 1)
    if is_thread_reply:
        first_q_text, thread_history, user_q_count = get_thread_context_and_count(channel, thread_ts)
        first_q_clean = re.sub(r'^\[질의-AI\]\s*', '', first_q_text).strip()
    else:
        first_q_clean = clean_prompt

    wants_manual_escalate = check_manual_escalation_intent(clean_prompt)
    is_escalation_triggered = (user_q_count >= 3) or wants_manual_escalate

    search_target = f"{first_q_clean} {clean_prompt}" if is_thread_reply else clean_prompt
    knowledge = search_relevant_knowledge(search_target)

    if is_escalation_triggered:
        escalation_guide_prompt = """
*중요 특별 지침 (관리자/팀장/SV 에스컬레이션 모드)*:
상담원이 질문을 3회 이상 이어가고 있거나, 팀장/관리자/SV 확인을 직접 원하고 있습니다.
AI가 모든 결론을 단정 짓지 마세요. 
"해당 문의는 건별 특수 상황이나 예외적인 승인 기준이 필요할 수 있어, AI 지식만으로는 정확한 확인이 어려울 수 있습니다. 상담원님께서 안전하고 정확하게 처리하실 수 있도록 아래 [📋 관리자(SV) 질의 접수] 버튼을 통해 직접 확인해 보시는 것을 권장해 드립니다."라고 안내하세요.
"""
    else:
        escalation_guide_prompt = ""

    system_instruction = f"""
당신은 상담원들을 지원하는 따뜻하고 상냥한 사내 AI 어시스턴트입니다.

[⚠️ 최우선 검수 보정 지식 (팀장/관리자가 수정한 피드백)]:
{json.dumps(knowledge['corrected_rules'], ensure_ascii=False, indent=2)}
*중요*: 피드백이 두서없이 적혀 있더라도 핵심을 추출하여 명확하고 다정한 표준 규정 안내 양식으로 정리하세요.

[사내 마스터 규정 지식 베이스]:
{json.dumps(knowledge['master_rules'], ensure_ascii=False, indent=2)}

[이전 대화 맥락]:
{thread_history if thread_history else "신규 질문입니다."}

[현재 상담원 질문 (스레드 누적 {user_q_count}회차)]:
{clean_prompt}
{escalation_guide_prompt}

[답변 작성 가이드라인]:
1. 첫 문장은 상담원에게 힘을 주는 따뜻하고 밝은 인사로 시작하세요.
2. 규정 내용은 핵심 항목별로 이모티콘(📌, ✔️, 💡 등)과 볼드체(*텍스트*)를 사용하여 가독성 높게 정리하세요.
3. 스레드 연속 대화인 경우 이전 맥락을 이어 자연스럽고 일관되게 설명하세요.
4. 마무리는 다정하고 힘찬 응원으로 끝맺어 주세요.
"""

    answer = call_gemini_api(system_instruction)

    if slack_client:
        try:
            slack_client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
        except Exception:
            pass

        if is_escalation_triggered:
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": answer}},
                {"type": "divider"},
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📋 관리자(SV) 질의 접수"},
                            "style": "primary",
                            "action_id": "open_inquiry_modal_btn",
                            "value": target_ts
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "❌ 질의서 작성 안 함 (해결됨)"},
                            "action_id": "dismiss_inquiry_btn",
                            "value": target_ts
                        }
                    ]
                }
            ]
            post_kwargs = {"blocks": blocks}
        else:
            post_kwargs = {}

        try:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=target_ts,
                text=answer,
                **post_kwargs
            )
            slack_client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
        except Exception as e:
            print(f"❌ 슬랙 전송 실패: {e}")

    gubun_val = f"대댓글({user_q_count}회차)" if is_thread_reply else "최초답변"
    sub_q_val = clean_prompt if is_thread_reply else ""
    log_to_google_sheet(gubun_val, target_ts, user_id, first_q_clean, sub_q_val, answer)

# 11. Slack Events 엔드포인트
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

    if not bot_id and (text.startswith("[질의-AI]") or (thread_ts and thread_ts != ts)):
        background_tasks.add_task(process_slack_question, channel, ts, thread_ts, text, user_id)

    return {"status": "ok"}

# 12. 모달 제출 후 시트 처리 백그라운드 태스크
def bg_process_submission(view: dict):
    values = view.get("state", {}).get("values", {})
    meta = json.loads(view.get("private_metadata", "{}"))

    team = values.get("team_block", {}).get("team_input", {}).get("value", "")
    author = values.get("author_block", {}).get("author_input", {}).get("value", "")
    contract = values.get("contract_block", {}).get("contract_input", {}).get("value", "")
    name = values.get("name_block", {}).get("name_input", {}).get("value", "")
    customer_q = values.get("customerQ_block", {}).get("customerQ_input", {}).get("value", "")
    my_q = values.get("myQ_block", {}).get("myQ_input", {}).get("value", "")

    # Apps Script 엔드포인트 규격과 100% 일치
    sheet_payload = {
        "action": "escalate",
        "team": team,
        "author": author,
        "contract": contract,
        "name": name,
        "customerQ": customer_q,
        "myQ": my_q
    }

    if LOG_WEBHOOK_URL:
        try:
            requests.post(LOG_WEBHOOK_URL, json=sheet_payload, timeout=5)
        except Exception as e:
            print(f"⚠️ 대장 전송 실패: {e}")

    channel_id = meta.get("channel_id")
    thread_ts = meta.get("thread_ts")
    if slack_client and channel_id and thread_ts:
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"✅ *관리자(SV) 질의서가 접수 대장에 등록되었습니다.*\n• *소속:* {team} | *작성자:* {author} | *회원명:* {name}\n• *질의 내용:* {my_q}"
            )
        except Exception as e:
            print(f"⚠️ 완료 알림 실패: {e}")

# 13. Slack Interactivity 엔드포인트
@app.post("/slack/interactive")
async def slack_interactive(request: Request, background_tasks: BackgroundTasks):
    try:
        form_data = await request.form()
        payload_raw = form_data.get("payload")
        if not payload_raw:
            return Response(status_code=200)

        payload = json.loads(payload_raw)

        # 1) 버튼 클릭
        if payload.get("type") == "block_actions":
            for action in payload.get("actions", []):
                action_id = action.get("action_id")
                raw_val = action.get("value", "")

                # [관리자(SV) 질의 접수] 클릭 시 모달 열기
                if action_id == "open_inquiry_modal_btn":
                    trigger_id = payload.get("trigger_id")
                    channel_id = payload.get("channel", {}).get("id")
                    user_info = payload.get("user", {})
                    user_id = user_info.get("id")
                    user_name = user_info.get("name", "")

                    if slack_client:
                        try:
                            u_res = slack_client.users_info(user=user_id)
                            user_name = u_res.get("user", {}).get("real_name", user_name)
                        except Exception:
                            pass

                        modal_view = build_modal_view_dict(channel_id, raw_val, user_name)
                        api_res = slack_client.views_open(trigger_id=trigger_id, view=modal_view)
                        if not api_res.get("ok"):
                            print(f"❌ views.open 실패: {api_res.get('error')}")

                    return Response(status_code=200)

                # [질의서 작성 안 함] 클릭 시
                elif action_id == "dismiss_inquiry_btn":
                    channel_id = payload.get("channel", {}).get("id")
                    user_id = payload.get("user", {}).get("id")
                    if slack_client and channel_id:
                        try:
                            slack_client.chat_postMessage(
                                channel=channel_id,
                                thread_ts=raw_val,
                                text=f"👍 <@{user_id}> 상담원님, 문의가 해결되어 질의서 접수를 종료합니다. 오늘도 화이팅하세요! ✨"
                            )
                        except Exception as e:
                            print(f"⚠️ dismiss 메시지 발송 실패: {e}")
                    return Response(status_code=200)

        # 2) 모달 제출
        if payload.get("type") == "view_submission":
            view = payload.get("view", {})
            if view.get("callback_id") == "submit_inquiry_modal":
                background_tasks.add_task(bg_process_submission, view)
                return JSONResponse(content={"response_action": "clear"})

    except Exception as err:
        print(f"❌ interactive 엔드포인트 예외: {err}")
        return Response(status_code=200)

    return Response(status_code=200)
