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

def search_relevant_knowledge(query: str, top_k: int = 5) -> dict:
    global KNOWLEDGE_CACHE, CORRECTED_CACHE, LAST_SYNC_TIME
    if time.time() - LAST_SYNC_TIME > 60:
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

# 4. 볼드체 변환 유틸
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

# 7-1. 일상 대화 판별 (업무 키워드 포함 시 무조건 제외)
def check_casual_intent(text: str) -> bool:
    clean = re.sub(r'[^가-힣a-zA-Z0-9]', '', text.lower())
    
    # 🚨 업무 및 규정 질문 키워드는 절대 일상 대화로 오인하지 않음
    business_keywords = [
        "본인확인", "확인해야", "확인방법", "연체", "해약", "환급", "양도", 
        "명의", "프로세스", "절차", "서류", "발급", "신청", "인증", "기준", "어떻게"
    ]
    if any(b in clean for b in business_keywords):
        return False

    casual_keywords = [
        "안녕", "하이", "헬로", "hello", "hi", "좋은아침", "굿모닝", "퇴근", "출근",
        "고마워", "감사", "땡큐", "thanks", "수고", "고생", "화이팅", "파이팅", "힘내",
        "대박", "최고", "짱", "짱이다", "굿잡", "든든",
        "알겠", "오케이", "ok", "네", "넵", "넹", "응", "어", "ㅇㅇ", "굿", "굳", "좋아"
    ]
    if len(clean) <= 15 and any(k in clean for k in casual_keywords):
        return True
    return False

# 7-2. 관리자 질의 키워드 감지 유틸
def check_manual_escalation_intent(text: str) -> bool:
    keywords = ["팀장", "관리자", "sv", "에스브이", "상급자", "물어볼", "물어봐야", "물어볼래", "확인해볼", "올려볼", "질의서", "질문있어", "질문 있어"]
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
            "answer": answer,
            "feedback": "",
            "review_status": "검수 필요"
        }, timeout=5)
    except Exception as e:
        print(f"⚠️ 질의로그 시트 전송 실패: {e}")

# 9. 모달 UI 생성 함수
def build_modal_view_dict(channel_id: str, thread_ts: str, user_name: str, user_id: str) -> dict:
    return {
        "type": "modal",
        "callback_id": "submit_inquiry_modal",
        "title": {"type": "plain_text", "text": "질의 접수"},
        "submit": {"type": "plain_text", "text": "전송"},
        "close": {"type": "plain_text", "text": "취소"},
        "private_metadata": json.dumps({
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "user_name": user_name
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

    # 1) 일상 대화/인사/감사 감지 시 -> 단독 격려 응답 (버튼 미부착)
    if check_casual_intent(clean_prompt):
        casual_prompt = f"""
당신은 보람상조 양평센터 상담원들을 항상 곁에서 응원하고 돕는 친절하고 다정한 사내 AI 동료 '대성봇'입니다.
상담원의 인사나 리액션에 맞춰 기분 좋은 에너지와 따뜻한 공감을 담아 자연스럽게 화답해 주세요.

[응답 가이드]:
- 인사를 건네면 반갑고 활기차게 맞이해 주세요.
- 감사를 표하면 뿌듯한 마음과 함께 언제든 돕겠다는 다정한 멘트를 건네세요.
- 업무 마감이나 퇴근, 수고 인사를 하면 따뜻한 위로와 응원을 전하세요.
- 이모지(😊, ✨, 👍, 🍀 등)를 적절히 섞어 1~2줄 내외로 간결하고 기분 좋게 답변하세요.
- 불필요한 사과나 시스템적인 문구는 절대 넣지 마세요.

상담원의 말: "{clean_prompt}"
"""
        answer = call_gemini_api(casual_prompt)

        if slack_client:
            try:
                slack_client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
            except Exception:
                pass
            try:
                slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=target_ts,
                    text=answer
                )
                slack_client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
            except Exception as e:
                print(f"❌ 슬랙 전송 실패: {e}")

        log_to_google_sheet("일상대화", target_ts, user_id, clean_prompt, "", answer)
        return

    # 2) 정규 업무 질의 처리
    first_q_text, thread_history, user_q_count = ("", "", 1)
    if is_thread_reply:
        first_q_text, thread_history, user_q_count = get_thread_context_and_count(channel, thread_ts)
        first_q_clean = re.sub(r'^\[질의-AI\]\s*', '', first_q_text).strip()
    else:
        first_q_clean = clean_prompt

    wants_manual_escalate = check_manual_escalation_intent(clean_prompt)
    is_escalation_triggered = (user_q_count >= 3) or wants_manual_escalate

    if wants_manual_escalate:
        answer = "관리자(SV)님께 바로 질문하실 수 있도록 접수를 도와드리겠습니다.\n아래 *[📋 관리자(SV) 질의 접수]* 버튼을 눌러 질의서를 작성해 주세요!"
    else:
        search_target = f"{first_q_clean} {clean_prompt}" if is_thread_reply else clean_prompt
        knowledge = search_relevant_knowledge(search_target)

        corrected_context = ""
        if knowledge.get('corrected_rules'):
            corrected_context = f"""
[⚠️ 관리자 최우선 보정 지침 - 기존 마스터 규정보다 아래 내용을 무조건 최우선 적용할 것]:
{json.dumps(knowledge['corrected_rules'], ensure_ascii=False, indent=2)}
"""

        system_instruction = f"""
당신은 보람상조 양평센터 상담원들을 지원하는 사내 AI 어시스턴트입니다.
사내 규정 지식과 관리자 보정 지침을 바탕으로 실무적이고 정확하게 답변하세요.

{corrected_context}

[기본 사내 규정 지식]:
{json.dumps(knowledge['master_rules'], ensure_ascii=False, indent=2)}

[이전 대화 맥락]:
{thread_history if thread_history else "신규 질문입니다."}

[질문]:
{clean_prompt}

[답변 작성 원칙]:
1. [⚠️ 관리자 최우선 보정 지침]에 기재된 피드백이나 보정 내용이 있다면 기존 규정보다 무조건 1순위로 채택하여 답변하세요.
2. 질문에 '본인확인' 키워드가 있거나 관련 문의인 경우:
   - 반드시 **사내 본인확인 프로세스 기준(성함, 생년월일, 연락처, 회원번호 등 필수 확인 항목 및 대리인 인입 시 위임 기준)**을 단계별로 엄격하게 설명하세요.
3. 질문에 '프로세스', '절차', '순서', '방법', '단계' 등의 키워드가 포함된 경우:
   - 반드시 **실무자가 취해야 할 단계별 진행 순서(1단계 ➔ 2단계 ➔ 3단계...)**를 최우선으로 먼저 명확히 제시하세요.
   - 혜택 중단/복구, 약관 조항, 환급금 주의사항 등은 단계별 프로세스 하단에 **[참고 및 고객 안내 사항]** 형태로 구분하여 보조 정보로 제공하세요.
4. 상담사가 고객과 통화 중 바로 참고할 수 있도록 불릿 포인트와 볼드체를 활용해 핵심만 간결하게 작성하세요.
"""
        base_answer = call_gemini_api(system_instruction)

        if user_q_count >= 3:
            answer = f"{base_answer}\n\n---\n*안내*: 해당 문의는 상황에 따른 세부 확인이 필요할 수 있으니, 아래 버튼을 통해 관리자(SV)님께 직접 질의서를 접수해 보시는 것을 권장해 드립니다."
        else:
            answer = base_answer

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
                            "text": {"type": "plain_text", "text": "❌ 작성 취소 (해결됨)"},
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

# 12. 모달 제출 후 시트 전송 처리
def bg_process_submission(view: dict):
    values = view.get("state", {}).get("values", {})
    meta = json.loads(view.get("private_metadata", "{}"))

    team = values.get("team_block", {}).get("team_input", {}).get("value", "")
    author_text = values.get("author_block", {}).get("author_input", {}).get("value", "")
    contract = values.get("contract_block", {}).get("contract_input", {}).get("value", "")
    name = values.get("name_block", {}).get("name_input", {}).get("value", "")
    customer_q = values.get("customerQ_block", {}).get("customerQ_input", {}).get("value", "")
    my_q = values.get("myQ_block", {}).get("myQ_input", {}).get("value", "")

    channel_id = meta.get("channel_id")
    thread_ts = meta.get("thread_ts")
    user_id = meta.get("user_id", "")

    author_val = f"<@{user_id}>" if user_id else author_text

    sheet_payload = {
        "action": "escalate",
        "team": team,
        "author": author_val,
        "contract": contract,
        "name": name,
        "customerQ": customer_q,
        "myQ": my_q,
        "slack_thread_key": f"{channel_id}|{thread_ts}"
    }

    if LOG_WEBHOOK_URL:
        try:
            requests.post(LOG_WEBHOOK_URL, json=sheet_payload, timeout=5)
        except Exception as e:
            print(f"⚠️ 대장 전송 실패: {e}")

    if slack_client and channel_id and thread_ts:
        try:
            slack_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text="전송 완료. 관리자가 확인하고 답변을 드릴 때 까지 1~2일 정도 소요될 수 있는 점 양해 부탁 드려요~"
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

                if action_id == "open_inquiry_modal_btn":
                    trigger_id = payload.get("trigger_id")
                    channel_id = payload.get("channel", {}).get("id")
                    user_info = payload.get("user", {})
                    user_id = user_info.get("id")
                    user_name = user_info.get("name", "상담사")

                    if slack_client and trigger_id:
                        try:
                            u_info = slack_client.users_info(user=user_id)
                            if u_info.get("ok"):
                                profile = u_info["user"].get("profile", {})
                                user_name = profile.get("display_name") or profile.get("real_name") or u_info["user"].get("real_name") or user_name
                        except Exception as e:
                            print(f"⚠️ users_info 조회 예외: {e}")

                        modal_view = build_modal_view_dict(channel_id, raw_val, user_name, user_id)
                        try:
                            api_res = slack_client.views_open(trigger_id=trigger_id, view=modal_view)
                            if not api_res.get("ok"):
                                print(f"❌ views.open 실패: {api_res.get('error')}")
                            else:
                                print(f"✅ 모달 팝업 성공! (실명: {user_name}, ID: {user_id})")
                        except Exception as e:
                            print(f"❌ views_open 호출 예외: {e}")

                    return Response(status_code=200)

                elif action_id == "dismiss_inquiry_btn":
                    channel_id = payload.get("channel", {}).get("id")
                    user_id = payload.get("user", {}).get("id")
                    
                    message_info = payload.get("message", {})
                    message_ts = message_info.get("ts")
                    original_text = message_info.get("text", "")

                    if slack_client and channel_id:
                        # 1단계: 기존 메시지에서 버튼 블록(blocks) 제거
                        if message_ts:
                            try:
                                slack_client.chat_update(
                                    channel=channel_id,
                                    ts=message_ts,
                                    text=original_text,
                                    blocks=[
                                        {"type": "section", "text": {"type": "mrkdwn", "text": original_text}}
                                    ]
                                )
                            except Exception as e:
                                print(f"⚠️ 버튼 제거 실패: {e}")

                        # 2단계: 종료 알림 댓글 발송
                        try:
                            slack_client.chat_postMessage(
                                channel=channel_id,
                                thread_ts=raw_val,
                                text=f"👍 <@{user_id}> 상담원님, 문의가 해결되어 질의서 접수를 종료합니다."
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
